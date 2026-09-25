"""企业连接器运行时（docs/04 §4，M3）：把已登记连接器的端点包装为平台 Tool。

- rest：httpx 按 endpoint 的 method/path 调外部系统（GET → query，POST/PUT → JSON body）
- mock-erp：内置离线演示 ERP（库存查询/下单），供开发与测试
- sql（M31 任务组 C）：只读 SELECT 查询（白名单校验 + 命名参数绑定 + 行数上限），
  sqlite 首要落地；postgresql 走 psycopg sync 连接（M55-D：postgres extra 提供驱动，
  分支内惰性 import；DSN 经 config.database=env:VAR_NAME 环境变量引用注入，不落库——
  config 列为 JSON 明文，DSN 含密码直接落库即泄密面）；其它 dialect 未装驱动时
  运行时报清晰错误
- OAuth2 凭证托管（M31 任务组 C）：client_credentials / authorization_code / refresh_token，
  token HTTP 经可注入 transport（http_client_factory，测试替换为离线假件）

工具名取 endpoint.tool_name（如 erp.order.create），requires_approval 透传给 HITL 门控。
所有连接器工具调用路径统一发射 connector.invoked 事件（try/except 不阻断）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..models import ConnectorRecord
from .tools import Tool

_TIMEOUT = 10.0
_HEALTH_TIMEOUT = 5.0

# 自定义连接器类型（v0.7-Connector SDK）：kind → factory(record) -> list[Tool]
_CONNECTOR_KINDS: dict[str, callable] = {}


def register_connector_kind(kind: str, factory) -> None:
    """注册新连接器类型：factory(ConnectorRecord) -> list[Tool]（超出 rest/mock-erp 的形态）。"""
    if not kind or not isinstance(kind, str):
        raise ValueError("connector kind 非法")
    _CONNECTOR_KINDS[kind] = factory


# ---------- 可注入 HTTP transport（M31 任务组 C） ----------

def http_client_factory(timeout: float = _TIMEOUT):
    """HTTP 客户端工厂（默认 httpx，禁跟随重定向）：token 获取与 rest 调用共用。

    测试离线化：monkeypatch 本函数返回假客户端（async with 可用 + request/post 方法），
    禁真实网络。
    """
    import httpx

    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


def _utcnow() -> datetime:
    """naive UTC（与模型列 default=utcnow 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_url(base_url: str, path: str) -> str:
    """仅允许登记时写死的 http(s) base_url 拼接 path（SSRF 基线：拒绝其它 scheme）。"""
    scheme = urlparse(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"EAP-7002 连接器 base_url 须为 http(s)，当前 {base_url!r}")
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _mock_erp_handler(endpoint_name: str):
    """内置演示 ERP：确定性的库存查询与下单处理。"""

    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if endpoint_name == "inventory.query":
            product = str(args.get("product") or args.get("query") or "").strip()
            catalog = {"EAP 一体机": 15, "EAP 网关": 42, "EAP 传感器": 8}
            if not product:
                # 无查询条件 → 返回目录（交互引擎动态选项的数据源形态）
                return json.dumps({"options": [{"product": k, "stock": v, "unit": "台"}
                                               for k, v in catalog.items()]},
                                  ensure_ascii=False)
            stock = catalog.get(product, 8)
            return json.dumps({"product": product, "warehouse": "CN-EAST-1",
                               "stock": stock, "unit": "台"}, ensure_ascii=False)
        if endpoint_name == "order.create":
            product = str(args.get("product") or "未指定产品")
            order_id = "SO-2026-" + product[:6].upper().replace(" ", "")
            return json.dumps({"order_id": order_id, "status": "created",
                               "product": product, "qty": int(args.get("qty") or 1)},
                              ensure_ascii=False)
        return json.dumps({"error": f"mock-erp 未知端点 {endpoint_name}"}, ensure_ascii=False)

    return handler


def _rest_handler(record: ConnectorRecord, endpoint: dict):
    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        method = (endpoint.get("method") or "GET").upper()
        url = _safe_url(record.base_url, endpoint.get("path") or "")
        headers = {}
        if record.oauth_token_url:
            # OAuth2 托管（M31 任务组 C）：配置了 oauth 即带 Bearer（无有效 token 自动获取/刷新）
            token = await ensure_oauth_token(record.id)
            headers["Authorization"] = f"Bearer {token}"
        elif record.api_key:
            from ..security_crypto import decrypt_secret

            headers[record.header_name or "Authorization"] = decrypt_secret(record.api_key)
        async with http_client_factory(_TIMEOUT) as client:
            if method == "GET":
                resp = await client.request(method, url, params=args, headers=headers)
            else:
                resp = await client.request(method, url, json=args, headers=headers)
        return json.dumps({"status": resp.status_code,
                           "body": _safe_json(resp.text)}, ensure_ascii=False)

    return handler


# ---------- SQL 连接器 kind（M31 任务组 C；M55-D 增 postgresql/psycopg 运行时）：
#            只读白名单 + 命名参数绑定 + 行数上限 ----------

# 写关键字词边界匹配（防 xxxSELECT 之类的绕过由首词校验兜底；此处防语句中出现写动作。
# 不含 replace：replace() 是合法读函数，REPLACE INTO 写语句已被首词校验拒绝）
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|grant|revoke)\b",
    re.IGNORECASE)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_FIRST_WORD = re.compile(r"^[a-zA-Z_]+")
_SQL_NAMED_PARAM = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")
# 负向后行 (?<!:)：postgresql 的 ::type 强转（如 id::text）不被误当命名占位
# （sqlite 无 :: 语法，既有查询不受影响）


def _validate_readonly_sql(query: str) -> str:
    """只读白名单校验，返回可执行 SQL（剥注释 + 去末尾分号）。

    - 剥注释/首尾空白后首词必须为 SELECT（xxxSELECT 之类的拼接绕过在此拒绝）
    - 全文词边界匹配写关键字（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/DETACH/
      PRAGMA/VACUUM/REPLACE/GRANT/REVOKE）任一出现即拒绝（含字符串字面量，fail-closed）
    - 拒绝多语句：仅允许末尾单个分号
    """
    cleaned = _SQL_BLOCK_COMMENT.sub(" ", query or "")
    cleaned = _SQL_LINE_COMMENT.sub(" ", cleaned)
    stripped = cleaned.strip()
    if not stripped:
        raise ValueError("EAP-7003 SQL 连接器端点缺少 query")
    first = _SQL_FIRST_WORD.match(stripped)
    if first is None or first.group(0).lower() != "select":
        got = first.group(0) if first else stripped[:20]
        raise ValueError(f"EAP-7003 SQL 连接器仅允许只读 SELECT，首词 {got!r} 非法")
    hit = _SQL_FORBIDDEN.search(stripped)
    if hit:
        raise ValueError(f"EAP-7003 只读连接器禁止关键字 {hit.group(0).upper()}")
    body = stripped[:-1].rstrip() if stripped.endswith(";") else stripped
    if ";" in body:
        raise ValueError("EAP-7003 拒绝多语句（禁止分号）")
    return body


def _sql_bind(query: str, args: dict, marker: str = "?") -> tuple[str, list]:
    """命名占位（:name）→ 位置绑定（sqlite ``?`` / postgresql ``%s``）：值不进 SQL 文本，杜绝注入。

    marker="%s"（psycopg）时先把查询里的字面 ``%`` 转义为 ``%%``（psycopg 参数化规则），
    再插 %s 占位——LIKE 'A%' 之类的字面百分号不会被误当占位前缀。
    """
    values: list = []
    missing: set[str] = set()

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in args:
            values.append(args[key])
            return marker
        missing.add(key)
        return m.group(0)

    source = query.replace("%", "%%") if marker != "?" else query
    converted = _SQL_NAMED_PARAM.sub(_repl, source)
    if missing:
        raise ValueError(f"EAP-7000 SQL 端点缺少命名参数: {', '.join(sorted(missing))}")
    return converted, values


def _sql_max_rows() -> int:
    """行数上限（Settings.connector_sql_max_rows，默认 200；取值为防御性下限 1）。"""
    from ..config import get_settings

    try:
        return max(1, int(get_settings().connector_sql_max_rows))
    except Exception:
        return 200


def _sqlite_config(record: ConnectorRecord) -> tuple[str, str]:
    """取 SQL 连接器配置：返回 (dialect, database)。

    database 语义随 dialect：sqlite 为库文件路径；postgresql 为 env:VAR_NAME 引用
    （DSN 经环境变量注入不落库，见 _resolve_pg_dsn）。函数名保留 sqlite 沿革。
    """
    cfg = record.config or {}
    dialect = str(cfg.get("dialect") or "sqlite").lower()
    database = str(cfg.get("database") or "")
    if not database:
        raise ValueError("EAP-7003 SQL 连接器 config.database 未配置")
    return dialect, database


# postgresql config.database 只接受 env:VAR_NAME 引用形态（M55-D 安全取舍）：
# config 列是 JSON 明文落库（api_key/oauth 才走 Fernet 加密链），DSN 含密码若直接
# 落库即泄密面——故一律经环境变量引用，运行时 os.environ 取值，DSN 不落库。
# 取舍理由：不动 sqlite 现状、零迁移、密钥面最小（方案 b 把 database 纳入加密需改列语义）。
_PG_ENV_DB = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")


def pg_env_var_name(database: str) -> str | None:
    """postgresql config.database 的 env:VAR 形态校验：合法返回变量名，否则 None。

    api/v1/connectors.py create/PATCH 校验与运行时解析共用同一规则（fail-closed 一致）。
    """
    m = _PG_ENV_DB.match(database or "")
    return m.group(1) if m else None


def _resolve_pg_dsn(database: str) -> str:
    """解析 postgresql DSN：env:VAR → os.environ 取值。

    缺变量 → EAP-7003 清晰报错（先于驱动 import 触发，无 psycopg 的环境同样可诊断）。
    """
    var = pg_env_var_name(database)
    if var is None:
        raise ValueError(
            "EAP-7003 postgresql 连接器 config.database 须为 env:VAR_NAME 引用形态"
            "（config 明文落库，DSN 含密码须经环境变量注入不落库；示例见 .env.example.prod）")
    dsn = os.environ.get(var)
    if not dsn:
        raise ValueError(
            f"EAP-7003 环境变量 {var} 未设置：postgresql 连接器 DSN 经 env:{var} 引用注入"
            "（在部署环境配置该变量后重启生效；URL 或 key=value 串均可）")
    return dsn


def _run_sqlite_query(database: str, sql: str, values: list, max_rows: int) -> dict:
    """sqlite 只读查询（线程内执行，供 to_thread/wait_for）：{columns, rows, truncated}。"""
    import sqlite3

    conn = sqlite3.connect(database, timeout=5.0)
    try:
        cur = conn.execute(sql, values)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchmany(max_rows + 1)]  # 多取 1 行判断截断
        truncated = len(rows) > max_rows
        return {"columns": columns, "rows": rows[:max_rows], "truncated": truncated}
    finally:
        conn.close()


def _run_pg_query(dsn: str, sql: str, values: list, max_rows: int) -> dict:
    """postgresql 只读查询（psycopg sync 连接，线程内执行供 to_thread/wait_for）。

    执行形态对齐 _run_sqlite_query：短连接 + 单语句 + fetchmany(max_rows+1) 判截断；
    连接阶段 connect_timeout=5s，查询阶段由外层 wait_for 兜底。只读纪律不靠驱动：
    上游 _validate_readonly_sql 白名单已拦截写语句/多语句，本层不再放行。
    驱动惰性 import：psycopg 不进基础依赖（postgres extra 提供），缺失 → 清晰错误。
    """
    try:
        import psycopg
    except ImportError:
        raise ValueError(
            "EAP-7003 dialect=postgresql 需要 psycopg 驱动（当前环境未安装；"
            "平台经 postgres extra 提供，安装后重启生效）") from None

    conn = psycopg.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            columns = [d.name for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchmany(max_rows + 1)]  # 多取 1 行判断截断
            truncated = len(rows) > max_rows
            return {"columns": columns, "rows": rows[:max_rows], "truncated": truncated}
    finally:
        conn.close()


def _require_dialect_driver(dialect: str) -> None:
    """sqlite/postgresql 之外的 dialect：运行时报清晰错误（不新增依赖）。

    sqlite 内置首要落地；postgresql 走 psycopg 运行时（M55-D）。其余 dialect 需先
    引入对应驱动并接入运行时（保持「驱动未安装/未接入」报错语义，不静默失败）。
    """
    raise ValueError(
        f"EAP-7003 SQL 连接器 dialect={dialect} 驱动未安装或未接入运行时"
        "（当前支持 sqlite 与 postgresql）")


def _sql_handler(record: ConnectorRecord, endpoint: dict):
    async def handler(arguments: str) -> str:
        sql = _validate_readonly_sql(endpoint.get("query") or "")
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("EAP-7000 SQL 端点参数须为 JSON 对象")
        dialect, database = _sqlite_config(record)
        if dialect == "sqlite":
            converted, values = _sql_bind(sql, args, "?")
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_sqlite_query, database, converted, values,
                                  _sql_max_rows()),
                timeout=_TIMEOUT)
        elif dialect == "postgresql":
            dsn = _resolve_pg_dsn(database)  # env:VAR → DSN；缺 env 先于驱动 import 报错
            converted, values = _sql_bind(sql, args, "%s")
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_pg_query, dsn, converted, values,
                                  _sql_max_rows()),
                timeout=_TIMEOUT)
        else:
            _require_dialect_driver(dialect)
        # default=str：postgresql 原生类型（timestamp/Decimal 等）可序列化（sqlite 原本全可序列化）
        return json.dumps(result, ensure_ascii=False, default=str)

    return handler


def _sql_tool(record: ConnectorRecord, endpoint: dict) -> Tool:
    name = endpoint.get("tool_name") or f"conn.{record.name}.{endpoint.get('name', '')}"
    return Tool(
        name=name,
        description=endpoint.get("description") or f"连接器 {record.name} 只读查询 {endpoint.get('name', '')}",
        parameters=_params_schema(endpoint),
        handler=_sql_handler(record, endpoint),
        requires_approval=bool(endpoint.get("requires_approval")),
    )


def _sql_tools(record: ConnectorRecord) -> list[Tool]:
    return [_sql_tool(record, ep) for ep in (record.endpoints or [])]


register_connector_kind("sql", _sql_tools)


async def sql_health_probe(record: ConnectorRecord) -> tuple[bool, str]:
    """SQL 连接器健康探测：执行 SELECT 1（5s 超时）。返回 (ok, detail)。

    sqlite / postgresql（psycopg，M55-D）均支持。detail 只含 sqlite 库路径或
    env:VAR 引用字面量，**不回显 DSN**（防密码泄漏到响应/审计）。
    """
    try:
        dialect, database = _sqlite_config(record)
        if dialect == "sqlite":
            await asyncio.wait_for(
                asyncio.to_thread(_run_sqlite_query, database, "SELECT 1", [], 1),
                timeout=_HEALTH_TIMEOUT)
            return True, f"sqlite SELECT 1 正常（{database}）"
        if dialect == "postgresql":
            dsn = _resolve_pg_dsn(database)
            await asyncio.wait_for(
                asyncio.to_thread(_run_pg_query, dsn, "SELECT 1", [], 1),
                timeout=_HEALTH_TIMEOUT)
            return True, f"postgresql SELECT 1 正常（{database}）"  # database=env:VAR 字面量，非 DSN
        _require_dialect_driver(dialect)
    except Exception as e:
        return False, str(e)[:200]


# ---------- OAuth2 凭证托管（M31 任务组 C） ----------

_TOKEN_MARGIN = 30  # 过期前 30s 即视为失效（防临界竞态）
_STATE_TTL = 600  # authorization_code state 有效期（秒，防 CSRF）

_oauth_states: dict[str, dict] = {}  # state → {name, redirect_uri, expires}（进程内，同 _seen_event_ids 语义）


def _oauth_token_scheme_ok(url: str) -> bool:
    return urlparse(url or "").scheme.lower() in ("http", "https")


async def _oauth_token_request(record: ConnectorRecord, form: dict) -> dict:
    """向 oauth_token_url 发 token 请求（经可注入 transport），校验并返回 JSON 响应。"""
    if not _oauth_token_scheme_ok(record.oauth_token_url or ""):
        raise ValueError(f"EAP-7004 OAuth token_url 须为 http(s)，当前 {record.oauth_token_url!r}")
    async with http_client_factory(_TIMEOUT) as client:
        resp = await client.post(record.oauth_token_url, data=form)
    if resp.status_code != 200:
        raise ValueError(f"EAP-7004 OAuth token 端点返回 HTTP {resp.status_code}")
    try:
        payload = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("EAP-7004 OAuth token 响应非 JSON") from e
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("EAP-7004 OAuth token 响应缺少 access_token")
    return payload


def _persist_oauth_tokens(record: ConnectorRecord, payload: dict) -> None:
    """token 响应 → 加密落库（access/refresh Fernet，expires_at = now + expires_in）。"""
    from ..security_crypto import encrypt_secret

    record.oauth_access_token_enc = encrypt_secret(str(payload["access_token"]))
    if payload.get("refresh_token"):
        record.oauth_refresh_token_enc = encrypt_secret(str(payload["refresh_token"]))
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    record.oauth_expires_at = _utcnow() + timedelta(seconds=expires_in)


async def ensure_oauth_token(record_id: int, force: bool = False) -> str:
    """确保连接器持有效 access token 并返回明文（工具调用前自动获取/过期自动刷新）。

    - 快路径：库存 token 未到 margin 期 → 直接返回（不经 HTTP）
    - 过期/缺失 → 有 refresh_token 走 refresh_token 刷新；否则 client_credentials 重取
      （authorization_code 首次取 token 走 /oauth/callback，落库后同样走快路径）
    - 持久化：access/refresh Fernet 加密；失败抛 ValueError（handler 层转 EAP-7004 语义）
    """
    from ..db import SessionLocal
    from ..security_crypto import decrypt_secret

    with SessionLocal() as db:
        record = db.get(ConnectorRecord, record_id)
        if record is None:
            raise ValueError(f"EAP-4004 连接器不存在（id={record_id}）")
        if not record.oauth_token_url:
            raise ValueError("EAP-7004 连接器未配置 OAuth token_url")
        now = _utcnow()
        access = decrypt_secret(record.oauth_access_token_enc) or ""
        if not force and access and record.oauth_expires_at is not None \
                and record.oauth_expires_at > now + timedelta(seconds=_TOKEN_MARGIN):
            return access
        refresh = decrypt_secret(record.oauth_refresh_token_enc) or ""
        if refresh:
            payload = await _oauth_token_request(record, {
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": record.oauth_client_id or "",
            })
        elif record.oauth_client_id and record.oauth_client_secret_enc:
            form = {"grant_type": "client_credentials",
                    "client_id": record.oauth_client_id,
                    "client_secret": decrypt_secret(record.oauth_client_secret_enc) or ""}
            if record.oauth_scopes:
                form["scope"] = record.oauth_scopes
            payload = await _oauth_token_request(record, form)
        else:
            raise ValueError("EAP-7004 连接器 OAuth 凭证不全：需 client_id/client_secret，"
                             "authorization_code 形态请先走 /oauth/authorize + /oauth/callback")
        _persist_oauth_tokens(record, payload)
        db.commit()
        return decrypt_secret(record.oauth_access_token_enc) or ""


def store_oauth_state(name: str, redirect_uri: str = "") -> str:
    """authorization_code：生成并登记一次性 state（CSRF 防护，进程内 TTL）。"""
    import uuid

    _prune_oauth_states()
    state = uuid.uuid4().hex
    _oauth_states[state] = {"name": name, "redirect_uri": redirect_uri,
                            "expires": time.time() + _STATE_TTL}
    return state


def pop_oauth_state(state: str, name: str) -> dict:
    """校验并消费 state（一次性）；不匹配/过期/重放 → ValueError。"""
    _prune_oauth_states()
    st = _oauth_states.pop(state or "", None)
    if st is None or st["name"] != name:
        raise ValueError("EAP-7004 OAuth state 无效或已过期（CSRF 防护拒绝）")
    return st


def _prune_oauth_states() -> None:
    now = time.time()
    for key in [k for k, v in _oauth_states.items() if v.get("expires", 0) < now]:
        _oauth_states.pop(key, None)


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:2000]


def _params_schema(endpoint: dict) -> dict:
    return endpoint.get("params") or {"type": "object", "properties": {}}


def _with_invoked_event(tool: Tool, record: ConnectorRecord) -> Tool:
    """包装 handler：调用路径发射 connector.invoked 事件（成功/失败均发，try/except 不阻断）。"""
    from .events import emit_event

    inner = tool.handler

    async def handler(arguments: str) -> str:
        status = "ok"
        try:
            return await inner(arguments)
        except Exception:
            status = "error"
            raise
        finally:
            try:
                emit_event("connector.invoked", getattr(record, "tenant_id", None),
                           {"connector": record.name, "endpoint": tool.name, "status": status})
            except Exception:
                pass

    return Tool(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        handler=handler,
        emits_citations=tool.emits_citations,
        requires_approval=tool.requires_approval,
        risk_level=tool.risk_level,
        timeout_s=tool.timeout_s,
    )


def endpoint_tool(record: ConnectorRecord, endpoint: dict) -> Tool:
    name = endpoint.get("tool_name") or f"conn.{record.name}.{endpoint.get('name', '')}"
    if record.kind in _CONNECTOR_KINDS:
        # 自定义连接器类型（v0.7-Connector SDK）：整记录交由扩展工厂构建工具集
        tools = _CONNECTOR_KINDS[record.kind](record)
        tool = next(t for t in tools if t.name == name)
    else:
        handler = _mock_erp_handler(endpoint.get("name", "")) if record.kind == "mock-erp" \
            else _rest_handler(record, endpoint)
        tool = Tool(
            name=name,
            description=endpoint.get("description") or f"连接器 {record.name} 端点 {name}",
            parameters=_params_schema(endpoint),
            handler=handler,
            requires_approval=bool(endpoint.get("requires_approval")),
        )
    return _with_invoked_event(tool, record)


def load_connector_tools(record: ConnectorRecord) -> list[Tool]:
    """把单个连接器的全部端点包装为平台 Tool（未启用返回空）。"""
    if not record.enabled:
        return []
    return [endpoint_tool(record, ep) for ep in (record.endpoints or [])]


def load_enabled_connector_tools(db) -> list[Tool]:
    """全部启用连接器的工具合集（智能体工具池用）。"""
    from sqlalchemy import select

    records = db.scalars(select(ConnectorRecord).where(ConnectorRecord.enabled == True)).all()  # noqa: E712
    tools: list[Tool] = []
    for record in records:
        tools.extend(load_connector_tools(record))
    return tools

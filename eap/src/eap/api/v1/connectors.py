"""企业连接器 API（docs/04 §4，M3）：登记 / 验证 / 启停 / 工具清单。

M31 任务组 C 增量：sql kind 登记（config={dialect, database}）、OAuth2 凭证托管
（authorize/callback/token 端点，secret Fernet 加密落库）、Health Check（探测落库 + 审计）。
M52-C 增量：GET /{name} 详情（编辑回填，secret 不回显仅出 has_* 布尔）+
PATCH /{name} 局部更新（name/kind 不可变；secret 三态：缺省保留 / "" 清除 / 非空加密覆盖；
合并后逐 kind 校验；base_url/config/endpoints 变更 → status 重置 pending；审计 connector.update）。
"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import ConnectorRecord
from ...runtime.connectors import load_connector_tools
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/connectors",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class EndpointDef(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,40}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,60}$",
                           description="平台工具全名，如 erp.order.create")
    method: str = Field(default="GET", pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")
    path: str = Field(default="/", max_length=200)
    description: str = ""
    requires_approval: bool = False
    params: dict | None = None
    query: str | None = Field(default=None, max_length=4000,
                              description="sql kind 专用：只读 SELECT（运行时白名单校验）")


class ConnectorCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    kind: str = Field(default="rest", pattern=r"^(rest|mock-erp|sql)$")
    description: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=256)
    header_name: str = "Authorization"
    api_key: str | None = None
    endpoints: list[EndpointDef] = Field(min_length=1, max_length=32)
    enabled: bool = True
    # sql kind 类型专属配置：{"dialect": "sqlite", "database": "<path>"}
    config: dict | None = None
    # OAuth2 凭证托管（M31 任务组 C）：secret Fernet 加密落库，查询不回显
    oauth_client_id: str | None = Field(default=None, max_length=256)
    oauth_client_secret: str | None = Field(default=None, max_length=512)
    oauth_token_url: str | None = Field(default=None, max_length=512)
    oauth_scopes: str | None = Field(default=None, max_length=512)


def _view(r: ConnectorRecord) -> dict:
    return {"name": r.name, "kind": r.kind, "description": r.description,
            "base_url": r.base_url, "status": r.status, "enabled": r.enabled,
            "endpoints": [e.get("tool_name") for e in (r.endpoints or [])],
            "created_at": str(r.created_at),
            "last_health_at": str(r.last_health_at) if r.last_health_at else None,
            "last_health_ok": r.last_health_ok}


@router.get("")
def list_connectors(db: Session = fastapi.Depends(get_db)):
    return [_view(r) for r in db.scalars(select(ConnectorRecord)).all()]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def create_connector(body: ConnectorCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 连接器 {body.name} 已注册")
    if body.kind == "rest" and not body.base_url.lower().startswith(("http://", "https://")):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7002 rest 连接器必须提供 http(s) base_url")
    if body.kind == "sql":
        if not (body.config or {}).get("database"):
            raise fastapi.HTTPException(status_code=400,
                                        detail="EAP-7003 sql 连接器必须提供 config.database")
        if any(not e.query for e in body.endpoints):
            raise fastapi.HTTPException(status_code=400,
                                        detail="EAP-7003 sql 连接器每个端点必须提供 query")
    from ...security_crypto import encrypt_secret

    record = ConnectorRecord(
        name=body.name, kind=body.kind, description=body.description,
        base_url=body.base_url, header_name=body.header_name,
        api_key=encrypt_secret(body.api_key),
        endpoints=[e.model_dump() for e in body.endpoints], enabled=body.enabled,
        config=body.config or {},
        oauth_client_id=body.oauth_client_id,
        oauth_client_secret_enc=encrypt_secret(body.oauth_client_secret),
        oauth_token_url=body.oauth_token_url,
        oauth_scopes=body.oauth_scopes,
    )
    db.add(record)
    db.commit()
    audit.record("connector.create", actor=audit.actor_of(request), target=body.name,
                 detail={"kind": body.kind,
                         "has_oauth": bool(body.oauth_token_url)}, trace_id=getattr(request.state, "trace_id", ""))
    return _view(record)


@router.post("/{name}/validate")
async def validate_connector(name: str, db: Session = fastapi.Depends(get_db)):
    """连通性验证：mock-erp 恒定可达；sql 执行 SELECT 1；rest 对 base_url 发一次 HEAD/GET 探测。"""
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    if record.kind == "mock-erp":
        record.status = "verified"
        db.commit()
        return {"name": record.name, "status": record.status, "detail": "内置演示 ERP，恒定可达"}
    if record.kind == "sql":
        from ...runtime.connectors import sql_health_probe

        ok, detail = await sql_health_probe(record)
        record.status = "verified" if ok else "unreachable"
        db.commit()
        return {"name": record.name, "status": record.status, "detail": detail}

    import httpx

    try:
        from ...runtime.connectors import _safe_url

        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            headers = {}
            if record.api_key:
                from ...security_crypto import decrypt_secret

                headers[record.header_name or "Authorization"] = decrypt_secret(record.api_key)
            resp = await client.get(_safe_url(record.base_url, "/"), headers=headers)
        record.status = "verified" if resp.status_code < 500 else "unreachable"
        detail = f"HTTP {resp.status_code}"
    except Exception as e:  # 网络/超时/DNS
        record.status = "unreachable"
        detail = str(e)[:200]
    db.commit()
    return {"name": record.name, "status": record.status, "detail": detail}


@router.post("/{name}/enabled", dependencies=[fastapi.Depends(require_admin)])
def toggle(name: str, enabled: bool, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    record.enabled = enabled
    db.commit()
    audit.record("connector.toggle", actor=audit.actor_of(request), target=name,
                 detail={"enabled": enabled}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "enabled": record.enabled}


@router.get("/{name}/tools")
def connector_tools(name: str, db: Session = fastapi.Depends(get_db)):
    """该连接器注入平台工具池的 OpenAI function-calling Schema。"""
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    return [t.schema() for t in load_connector_tools(record)]


# ---------- Health Check（M31 任务组 C）：探测结果落库 + 审计 ----------

def _get_record(db: Session, name: str) -> ConnectorRecord:
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    return record


@router.post("/{name}/health", dependencies=[fastapi.Depends(require_admin)])
async def health_check(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """健康探测：rest GET base_url（5s 超时，2xx=ok）；sql 执行 SELECT 1；mock-erp 恒 ok。

    结果落库 last_health_at/last_health_ok（列表 _view 透出）+ 审计 connector.health。
    """
    record = _get_record(db, name)
    from datetime import datetime, timezone

    ok, detail = False, ""
    if record.kind == "mock-erp":
        ok, detail = True, "内置演示 ERP，恒定可达"
    elif record.kind == "sql":
        from ...runtime.connectors import sql_health_probe

        ok, detail = await sql_health_probe(record)
    else:  # rest
        try:
            from ...runtime.connectors import _safe_url, http_client_factory

            headers = {}
            if record.api_key:
                from ...security_crypto import decrypt_secret

                headers[record.header_name or "Authorization"] = decrypt_secret(record.api_key)
            async with http_client_factory(5.0) as client:
                resp = await client.get(_safe_url(record.base_url, "/"), headers=headers)
            ok = resp.status_code < 400 and resp.status_code >= 200
            detail = f"HTTP {resp.status_code}"
        except Exception as e:  # 网络/超时/DNS
            detail = str(e)[:200]
    record.last_health_at = datetime.now(timezone.utc).replace(tzinfo=None)
    record.last_health_ok = ok
    db.commit()
    audit.record("connector.health", actor=audit.actor_of(request), target=name,
                 detail={"ok": ok, "detail": detail}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "ok": ok, "detail": detail,
            "last_health_at": str(record.last_health_at), "last_health_ok": record.last_health_ok}


# ---------- PATCH 编辑流（M52-C）：详情回填 + 局部更新 ----------

def _edit_view(r: ConnectorRecord) -> dict:
    """GET /{name} 完整可编辑视图（M52-C）：编辑对话框回填用。

    - endpoints 回**全对象数组**（列表页 _view 只回 tool_name 摘要）；
    - oauth_client_id/token_url/scopes 明文回显（对齐 create 入参，本就非回显禁忌）；
    - **secret 一律不回显**：api_key / oauth_client_secret_enc 不出现在响应里，
      改出 has_api_key / has_oauth_secret 存在性布尔。
    """
    return {"name": r.name, "kind": r.kind, "description": r.description,
            "base_url": r.base_url, "header_name": r.header_name,
            "enabled": r.enabled, "status": r.status, "config": r.config or {},
            "endpoints": r.endpoints or [],
            "has_api_key": bool(r.api_key),
            "oauth_client_id": r.oauth_client_id,
            "oauth_token_url": r.oauth_token_url,
            "oauth_scopes": r.oauth_scopes,
            "has_oauth_secret": bool(r.oauth_client_secret_enc)}


class ConnectorUpdate(BaseModel):
    """PATCH 入参（M52-C）：全字段可选，只有显式提交的字段被应用（exclude_unset）。

    - **name/kind 不可变**：模型不收这两个字段（多传按 pydantic 默认忽略）。
    - **secret 三态**（api_key / oauth_client_secret）：字段缺省 = 保留原值；
      显式提交空串 ``""`` = 清除（置 None）；非空 = encrypt_secret 加密覆盖。
    """

    description: str | None = Field(default=None, max_length=256)
    base_url: str | None = Field(default=None, max_length=256)
    header_name: str | None = None
    api_key: str | None = None
    endpoints: list[EndpointDef] | None = Field(default=None, min_length=1, max_length=32)
    enabled: bool | None = None
    config: dict | None = None
    oauth_client_id: str | None = Field(default=None, max_length=256)
    oauth_client_secret: str | None = Field(default=None, max_length=512)
    oauth_token_url: str | None = Field(default=None, max_length=512)
    oauth_scopes: str | None = Field(default=None, max_length=512)


@router.get("/{name}", dependencies=[fastapi.Depends(require_admin)])
def connector_detail(name: str, db: Session = fastapi.Depends(get_db)):
    """连接器详情（M52-C，admin）：编辑对话框回填用完整视图，secret 不回显。"""
    return _edit_view(_get_record(db, name))


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def update_connector(name: str, body: ConnectorUpdate, request: fastapi.Request,
                     db: Session = fastapi.Depends(get_db)):
    """局部更新连接器（M52-C，admin + 审计 connector.update）。

    语义：

    - name/kind 不可变（ConnectorUpdate 不收这两个字段，多传忽略）。
    - secret 三态：缺省 = 保留；显式 ``""`` = 清除（置 None）；非空 = encrypt_secret 覆盖。
    - 逐 kind 校验复用 create 语义，用**合并后的最终值**（现有记录 + 显式提交字段）：
      rest 须 http(s) base_url（EAP-7002）；sql 须 config.database 且每个端点有
      query（EAP-7003）。
    - **status 重置**：base_url/config/endpoints 任一变更 → status="pending"——
      连接目标变了须重新 validate，旧验证结果不再可信（诚实语义）。
    - 工具池无需刷新：runtime/connectors.load_connector_tools 实时读库，PATCH 即生效。
    - 审计 detail 只记提交的**字段名**，绝不记 secret 值。
    """
    record = _get_record(db, name)
    data = body.model_dump(exclude_unset=True)

    # 合并后最终值（显式提交覆盖库存），用于逐 kind 校验
    base_url = record.base_url if data.get("base_url") is None else data["base_url"]
    config = (record.config or {}) if data.get("config") is None else data["config"]
    endpoints = (record.endpoints or []) if data.get("endpoints") is None else data["endpoints"]
    if record.kind == "rest" and not base_url.lower().startswith(("http://", "https://")):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7002 rest 连接器必须提供 http(s) base_url")
    if record.kind == "sql":
        if not (config or {}).get("database"):
            raise fastapi.HTTPException(status_code=400,
                                        detail="EAP-7003 sql 连接器必须提供 config.database")
        if any(not (e or {}).get("query") for e in endpoints):
            raise fastapi.HTTPException(status_code=400,
                                        detail="EAP-7003 sql 连接器每个端点必须提供 query")

    from ...security_crypto import encrypt_secret

    # 连接目标变更判定（须在赋值前对比库存旧值）
    target_changed = (
        (data.get("base_url") is not None and data["base_url"] != record.base_url)
        or (data.get("config") is not None and data["config"] != (record.config or {}))
        or (body.endpoints is not None and "endpoints" in data
            and [e.model_dump() for e in body.endpoints] != (record.endpoints or []))
    )

    for field in ("description", "base_url", "header_name",
                  "oauth_client_id", "oauth_token_url", "oauth_scopes"):
        if data.get(field) is not None:
            setattr(record, field, data[field])
    if data.get("enabled") is not None:
        record.enabled = data["enabled"]
    if data.get("config") is not None:
        record.config = data["config"]
    if body.endpoints is not None and "endpoints" in data:
        record.endpoints = [e.model_dump() for e in body.endpoints]  # 对齐 create 存储
    # secret 三态：键出现在显式提交集才动；空串/None = 清除，非空 = 加密覆盖
    if "api_key" in data:
        record.api_key = encrypt_secret(data["api_key"]) if data["api_key"] else None
    if "oauth_client_secret" in data:
        record.oauth_client_secret_enc = (encrypt_secret(data["oauth_client_secret"])
                                          if data["oauth_client_secret"] else None)
    if target_changed:
        record.status = "pending"  # 连接目标变了须重新 validate
    db.commit()
    audit.record("connector.update", actor=audit.actor_of(request), target=name,
                 detail={"fields": sorted(data), "has_oauth": bool(record.oauth_token_url)},
                 trace_id=getattr(request.state, "trace_id", ""))
    return _view(record)


# ---------- OAuth2 凭证托管（M31 任务组 C）：authorize / callback / token ----------

class OAuthCallback(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=1, max_length=128)


class OAuthTokenRequest(BaseModel):
    refresh: bool = Field(default=False, description="true = 忽略库存 token 强制刷新/重取")


def _derive_authorize_url(token_url: str) -> str:
    """从 token_url 推导 authorize 端点（…/token → …/authorize）；推不动则要求显式传入。"""
    base = (token_url or "").rstrip("/")
    if base.endswith("/token"):
        return base[: -len("token")] + "authorize"
    raise fastapi.HTTPException(
        status_code=400,
        detail="EAP-7004 无法从 token_url 推导 authorize 端点，请显式传 authorize_url 参数")


@router.get("/{name}/oauth/authorize", dependencies=[fastapi.Depends(require_admin)])
def oauth_authorize(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db),
                    redirect_uri: str = "", authorize_url: str = ""):
    """authorization_code 第一步：生成 IdP 跳转 URL + 登记一次性 state（CSRF 防护）。"""
    record = _get_record(db, name)
    if not record.oauth_client_id or not record.oauth_token_url:
        raise fastapi.HTTPException(status_code=400,
                                    detail="EAP-7004 连接器未配置 OAuth client_id/token_url")
    from urllib.parse import urlencode

    from ...runtime.connectors import store_oauth_state

    state = store_oauth_state(name, redirect_uri)
    params = {"response_type": "code", "client_id": record.oauth_client_id, "state": state}
    if record.oauth_scopes:
        params["scope"] = record.oauth_scopes
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    url = authorize_url or _derive_authorize_url(record.oauth_token_url)
    url += ("&" if "?" in url else "?") + urlencode(params)
    audit.record("connector.oauth.authorize", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "authorize_url": url, "state": state, "expires_in": 600}


@router.post("/{name}/oauth/callback", dependencies=[fastapi.Depends(require_admin)])
async def oauth_callback(name: str, body: OAuthCallback, request: fastapi.Request,
                         db: Session = fastapi.Depends(get_db)):
    """authorization_code 第二步：校验一次性 state（防 CSRF/重放）→ code 换 token 加密落库。"""
    record = _get_record(db, name)
    from ...security_crypto import decrypt_secret
    from ...runtime.connectors import _oauth_token_request, _persist_oauth_tokens, pop_oauth_state

    try:
        state_data = pop_oauth_state(body.state, name)  # 不匹配/过期/重放 → 400
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    form = {"grant_type": "authorization_code", "code": body.code,
            "client_id": record.oauth_client_id or "",
            "client_secret": decrypt_secret(record.oauth_client_secret_enc) or ""}
    if state_data.get("redirect_uri"):
        form["redirect_uri"] = state_data["redirect_uri"]
    try:
        payload = await _oauth_token_request(record, form)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=502, detail=str(e)) from e
    _persist_oauth_tokens(record, payload)
    db.commit()
    audit.record("connector.oauth.callback", actor=audit.actor_of(request), target=name,
                 detail={"status": "bound"}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "status": "bound", "expires_at": str(record.oauth_expires_at),
            "has_refresh_token": bool(record.oauth_refresh_token_enc)}


@router.post("/{name}/oauth/token", dependencies=[fastapi.Depends(require_admin)])
async def oauth_token(name: str, body: OAuthTokenRequest, request: fastapi.Request,
                      db: Session = fastapi.Depends(get_db)):
    """手动获取/刷新 token（admin）：client_credentials 或 refresh_token。

    响应不回显完整 token（仅回 expires_at / 是否持有 refresh token）。
    """
    record = _get_record(db, name)
    from ...runtime.connectors import ensure_oauth_token

    try:
        await ensure_oauth_token(record.id, force=body.refresh)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=502, detail=str(e)) from e
    db.expire(record)  # ensure 在独立会话提交，重读最新持久化状态
    audit.record("connector.oauth.token", actor=audit.actor_of(request), target=name,
                 detail={"refresh": body.refresh}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "status": "ok", "token_type": "Bearer",
            "expires_at": str(record.oauth_expires_at) if record.oauth_expires_at else None,
            "has_refresh_token": bool(record.oauth_refresh_token_enc)}

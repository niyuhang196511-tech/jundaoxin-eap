"""M55-D：SQL 连接器 psycopg（postgresql）运行时测试。

分两层（对齐 test_rls_behavior 的「非目标环境整文件 skip」思路，此处只 skip 活体组）：

- **非 PG 环境必测**（无容器也绿，SQLite 全量可跑）：
  - 登记/PATCH 校验：dialect=postgresql 时 config.database 必须为 env:VAR_NAME 引用
    形态——裸 DSN（URL 或 key=value 串）→ 400 EAP-7003（config 列 JSON 明文落库，
    DSN 含密码直接落库即泄密面，故经环境变量注入不落库）；
  - 落库字面量：库里存的是 ``env:VAR`` 原文，DSN 串不出现在任何响应里；
  - 缺 env：运行时 os.environ 取不到 → 503 EAP-7003 指明变量名（健康探测 ok=False 同样清晰）；
  - 驱动缺失：sys.modules 注入 None 模拟未装 psycopg → 清晰报错（不裸抛 ImportError）；
  - 只读白名单对 postgresql 同样生效：INSERT/DELETE/多语句/粘词首词全部拦截
    （校验先于 DSN 解析，无需容器即可断言）。
- **活体验证**（skipif 环境变量 EAP_M55_PG_DSN 未设置时整组 skip）：
  本批验证由人工先起临时容器（CI 无此环境自动 skip）：
  ::

    docker run -d --name eap-m55-pg -e POSTGRES_USER=eap -e POSTGRES_PASSWORD=m55pg \\
      -e POSTGRES_DB=eap_m55 -p 55434:5432 postgres:16-alpine
    EAP_M55_PG_DSN='postgresql://eap:m55pg@127.0.0.1:55434/eap_m55' uv run pytest tests/test_connector_pg.py -q
    docker rm -f eap-m55-pg   # 验证完即删

  覆盖：建表插数 → 连接器端点真查（列名/行数据/命名参数绑定）、``::`` 强转不被
  误当占位、LIKE 字面 ``%`` 经 ``%%`` 转义、timestamp 原生类型可序列化（default=str）、
  行数上限截断、health/validate 探测（detail 不回显 DSN）、写语句拦截后行数不变。

全部用例脏库可重入：连接器/工具名带 uuid 后缀唯一；活体建表先 DROP IF EXISTS。
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

from eap.config import get_settings

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

# ---------- 可重入唯一名（module 级一次性后缀，样板同 test_connector_v2） ----------

_SFX = uuid.uuid4().hex[:8]

# 活体容器 DSN 的环境变量名（config.database 即填 env:EAP_M55_PG_DSN，运行时同进程取值）
_PG_DSN_ENV = "EAP_M55_PG_DSN"
_LIVE_DSN = os.environ.get(_PG_DSN_ENV, "")

# 非 PG 必测组用的环境变量名（须未设置/由 monkeypatch 控制）
_PG_LIT_VAR_A = "EAP_M55_PG_LIT_A"      # 落库字面量组：登记用 VAR（只登记不解析，值无所谓）
_PG_LIT_VAR_B = "EAP_M55_PG_LIT_B"      # 落库字面量组：PATCH 后 VAR（确有 config 变更）
_PG_OFF_VAR = "EAP_M55_PG_OFFLINE_DSN"  # 缺 env 组：断言未设置时的清晰报错
_PG_DRV_VAR = "EAP_M55_PG_DRV_DSN"      # 驱动缺失组：setenv 假 DSN，让解析通过后卡驱动

PG_NAME = f"m55-pg-live-{_SFX}"      # 活体连接器
T_BY_NAME = f"m55-{_SFX}.pg.by_name"
T_ALL = f"m55-{_SFX}.pg.all"
T_LIKE = f"m55-{_SFX}.pg.like_pct"
T_CAST = f"m55-{_SFX}.pg.cast"
T_TS = f"m55-{_SFX}.pg.ts"
T_INS = f"m55-{_SFX}.pg.insert"  # 活体写语句拦截（拦截后行数不变的真实证据）

PG_BAD_NAME = f"m55-pg-bad-{_SFX}"   # 非活体只读白名单组
T_BAD_INSERT = f"m55-{_SFX}.pgbad.insert"
T_BAD_DELETE = f"m55-{_SFX}.pgbad.delete"
T_BAD_MULTI = f"m55-{_SFX}.pgbad.multi"
T_BAD_GLUE = f"m55-{_SFX}.pgbad.glue"

PG_OFF_NAME = f"m55-pg-off-{_SFX}"   # 缺 env 组
T_OFFLINE = f"m55-{_SFX}.pg.offline"

PG_DRV_NAME = f"m55-pg-drv-{_SFX}"   # 驱动缺失组
T_DRV = f"m55-{_SFX}.pg.drv"

PG_LIT_NAME = f"m55-pg-lit-{_SFX}"   # 落库字面量组
T_LIT = f"m55-{_SFX}.pg.lit"

# 活体组：EAP_M55_PG_DSN 未设置 → skip（CI / 无容器环境）
PG_LIVE = pytest.mark.skipif(not _LIVE_DSN, reason="EAP_M55_PG_DSN 未设置（无临时 PG 容器），跳过活体验证")


def _invoke(client, tool_name: str, args: dict | None = None):
    """经扩展中心工具试运行端点调用连接器工具（样板同 test_connector_v2）。"""
    return client.post(f"/api/v1/extensions/tools/{tool_name}/invoke",
                       headers=HEADERS, json={"args": args or {}})


# ---------- 活体夹具：容器内建表插数 + 登记活体连接器 ----------

@pytest.fixture(scope="module")
def pg_live(client):
    psycopg = pytest.importorskip("psycopg")

    conn = psycopg.connect(_LIVE_DSN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS m55_products")  # 容器可重入
            cur.execute("CREATE TABLE m55_products ("
                        "id INTEGER PRIMARY KEY, name TEXT, stock INTEGER)")
            cur.executemany("INSERT INTO m55_products (id, name, stock) VALUES (%s, %s, %s)",
                            [(1, "EAP 一体机", 15), (2, "EAP 网关", 42), (3, "EAP 传感器", 8)])
        conn.commit()
    finally:
        conn.close()

    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": PG_NAME, "kind": "sql",
        "description": "M55-D psycopg 活体验证连接器",
        "config": {"dialect": "postgresql", "database": f"env:{_PG_DSN_ENV}"},
        "endpoints": [
            {"name": "by_name", "tool_name": T_BY_NAME,
             "query": "SELECT id, name, stock FROM m55_products WHERE name = :name",
             "params": {"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]}},
            {"name": "all", "tool_name": T_ALL,
             "query": "SELECT id, name, stock FROM m55_products ORDER BY id"},
            {"name": "like_pct", "tool_name": T_LIKE,
             "query": "SELECT id FROM m55_products WHERE name LIKE 'EAP%'"},
            {"name": "cast", "tool_name": T_CAST,
             "query": "SELECT id::text AS id_text FROM m55_products WHERE id = :id",
             "params": {"type": "object", "properties": {"id": {"type": "integer"}},
                        "required": ["id"]}},
            {"name": "ts", "tool_name": T_TS,
             "query": "SELECT now() AS ts"},
            {"name": "insert", "tool_name": T_INS,
             "query": "INSERT INTO m55_products (id, name, stock) VALUES (9, 'hack', 0)"},
        ]})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def pg_lit(client):
    """登记 env:VAR 形态 postgresql 连接器（VAR 未设置：只走校验/落库路径，不解析 DSN）。"""
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": PG_LIT_NAME, "kind": "sql",
        "config": {"dialect": "postgresql", "database": f"env:{_PG_LIT_VAR_A}"},
        "endpoints": [{"name": "lit", "tool_name": T_LIT, "query": "SELECT 1"}]})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 非 PG 环境必测（无容器也绿） ----------

def test_pg_registration_requires_env_form(client, pg_lit):
    """postgresql 裸 DSN（URL / key=value）→ 400；env:VAR 形态 → 登记成功（夹具）。"""
    for i, raw in enumerate(("postgresql://eap:pw@db:5432/x",
                             "host=db dbname=x user=eap password=pw")):
        resp = client.post("/api/v1/connectors", headers=HEADERS, json={
            "name": f"m55-pg-raw-{_SFX}-{i}", "kind": "sql",
            "config": {"dialect": "postgresql", "database": raw},
            "endpoints": [{"name": f"q{i}", "tool_name": f"m55-{_SFX}.raw{i}.q",
                           "query": "SELECT 1"}]})
        assert resp.status_code == 400, resp.text
        assert "EAP-7003" in resp.json()["detail"] and "env:" in resp.json()["detail"]
    # env:VAR 形态登记成功（登记不连接，离线确定）
    assert pg_lit["kind"] == "sql" and pg_lit["status"] == "registered"
    # sqlite 形态不受影响（回归对照）
    assert client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": f"m55-sql-lite-{_SFX}", "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
        "endpoints": [{"name": "lite", "tool_name": f"m55-{_SFX}.lite.q",
                       "query": "SELECT 1"}]}).status_code == 200


def test_pg_patch_requires_env_form_merged_value(client, pg_lit):
    """PATCH（M52-C 合并后值校验语义保持）：postgresql 裸 DSN → 400；env:VAR → 200。"""
    resp = client.patch(f"/api/v1/connectors/{PG_LIT_NAME}", headers=AUTH,
                        json={"config": {"dialect": "postgresql",
                                         "database": "postgresql://u:p@h/db"}})
    assert resp.status_code == 400 and "env:" in resp.json()["detail"]
    # 提交 config 缺 database → 合并后值即提交值 → 仍 400（必须提供）
    resp = client.patch(f"/api/v1/connectors/{PG_LIT_NAME}", headers=AUTH,
                        json={"config": {"dialect": "postgresql"}})
    assert resp.status_code == 400 and "config.database" in resp.json()["detail"]
    # env:VAR 形态 → 200（VAR_A → VAR_B 确有 config 变更；status 重置语义归 M52-C 用例）
    resp = client.patch(f"/api/v1/connectors/{PG_LIT_NAME}", headers=AUTH,
                        json={"config": {"dialect": "postgresql",
                                         "database": f"env:{_PG_LIT_VAR_B}"}})
    assert resp.status_code == 200, resp.text


def test_pg_config_stores_env_literal_not_dsn(client, pg_lit):
    """落库的是 env:VAR 字面量；DSN 串不出现在响应（config 明文列的泄密面防线）。"""
    # 显式 PATCH 一次保证确定性终态（与执行顺序无关：VAR_B == VAR_B 时为无变更 200）
    resp = client.patch(f"/api/v1/connectors/{PG_LIT_NAME}", headers=AUTH,
                        json={"config": {"dialect": "postgresql",
                                         "database": f"env:{_PG_LIT_VAR_B}"}})
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/v1/connectors/{PG_LIT_NAME}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg == {"dialect": "postgresql", "database": f"env:{_PG_LIT_VAR_B}"}
    assert "postgresql://" not in resp.text


def test_pg_missing_env_clear_error(client, monkeypatch):
    """缺 env：解析先于驱动 import → 无 psycopg 环境同样得到 EAP-7003 + 变量名。"""
    monkeypatch.delenv(_PG_OFF_VAR, raising=False)  # 保证未设置（防外部导出串扰）
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": PG_OFF_NAME, "kind": "sql",
        "config": {"dialect": "postgresql", "database": f"env:{_PG_OFF_VAR}"},
        "endpoints": [{"name": "offline", "tool_name": T_OFFLINE, "query": "SELECT 1"}]})
    assert resp.status_code == 200, resp.text
    # 工具执行 → 503 EAP-7003，报文指明变量名
    resp = _invoke(client, T_OFFLINE)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "EAP-7003" in detail and _PG_OFF_VAR in detail
    # 健康探测 → ok=False，detail 同样清晰
    resp = client.post(f"/api/v1/connectors/{PG_OFF_NAME}/health", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["ok"] is False
    assert "EAP-7003" in resp.json()["detail"] and _PG_OFF_VAR in resp.json()["detail"]


def test_pg_driver_missing_clear_error(client, monkeypatch):
    """psycopg 缺失（sys.modules 注入 None 模拟）→ 清晰报错，不裸抛 ImportError。"""
    monkeypatch.setenv(_PG_DRV_VAR, "postgresql://eap:dummy@127.0.0.1:59999/x")
    monkeypatch.setitem(sys.modules, "psycopg", None)  # None in sys.modules → import 即 ImportError
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": PG_DRV_NAME, "kind": "sql",
        "config": {"dialect": "postgresql", "database": f"env:{_PG_DRV_VAR}"},
        "endpoints": [{"name": "drv", "tool_name": T_DRV, "query": "SELECT 1"}]})
    assert resp.status_code == 200, resp.text
    resp = _invoke(client, T_DRV)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "EAP-7003" in detail and "psycopg" in detail


def test_pg_readonly_whitelist(client):
    """只读白名单对 postgresql 同样生效：校验先于 DSN 解析，无需容器即可断言。"""
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": PG_BAD_NAME, "kind": "sql",
        "config": {"dialect": "postgresql", "database": f"env:{_PG_OFF_VAR}"},
        "endpoints": [
            {"name": "insert", "tool_name": T_BAD_INSERT,
             "query": "INSERT INTO t VALUES (1)"},
            {"name": "delete", "tool_name": T_BAD_DELETE,
             "query": "DELETE FROM t"},
            {"name": "multi", "tool_name": T_BAD_MULTI,
             "query": "SELECT 1; SELECT 2"},  # 无写关键字，命中多语句检查（DROP 会先撞词表）
            {"name": "glue", "tool_name": T_BAD_GLUE,
             "query": "xxxSELECT 1"},
        ]})
    assert resp.status_code == 200, resp.text
    # INSERT/DELETE：写关键字词边界拦截；multi：拒绝多语句；glue：粘词首词拒绝
    for tool, kw in [(T_BAD_INSERT, "INSERT"), (T_BAD_DELETE, "DELETE"),
                     (T_BAD_MULTI, "多语句"), (T_BAD_GLUE, "EAP-7003")]:
        resp = _invoke(client, tool)
        assert resp.status_code == 503, f"{tool}: {resp.text}"
        assert kw in resp.json()["detail"], resp.json()["detail"]


# ---------- 活体验证（EAP_M55_PG_DSN 指向临时容器时才跑） ----------

@PG_LIVE
def test_pg_live_query_rows_and_params(client, pg_live):
    resp = _invoke(client, T_BY_NAME, {"name": "EAP 网关"})
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert result["columns"] == ["id", "name", "stock"]
    assert result["rows"] == [[2, "EAP 网关", 42]]
    assert result["truncated"] is False
    # 无参数端点：全表 3 行
    resp = _invoke(client, T_ALL)
    assert len(json.loads(resp.json()["output"])["rows"]) == 3
    # 缺命名参数 → EAP-7000
    resp = _invoke(client, T_BY_NAME)
    assert resp.status_code == 503 and "EAP-7000" in resp.json()["detail"]


@PG_LIVE
def test_pg_live_cast_and_like_percent(client, pg_live):
    # ::text 强转不被误当命名占位（负向后行 (?<!:) 的活体证据）
    resp = _invoke(client, T_CAST, {"id": 1})
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert result["columns"] == ["id_text"] and result["rows"] == [["1"]]
    # LIKE 'EAP%' 字面百分号经 %% 转义，不被误当 %s 占位
    resp = _invoke(client, T_LIKE)
    assert resp.status_code == 200, resp.text
    assert len(json.loads(resp.json()["output"])["rows"]) == 3


@PG_LIVE
def test_pg_live_native_type_serialized(client, pg_live):
    # timestamp 原生类型经 json.dumps(default=str) 可序列化（不炸 TypeError）
    resp = _invoke(client, T_TS)
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert result["columns"] == ["ts"]
    assert result["rows"] and isinstance(result["rows"][0][0], str) and result["rows"][0][0]


@PG_LIVE
def test_pg_live_row_limit_truncation(client, pg_live, monkeypatch):
    monkeypatch.setattr(get_settings(), "connector_sql_max_rows", 2)
    resp = _invoke(client, T_ALL)
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert len(result["rows"]) == 2 and result["truncated"] is True
    assert result["columns"] == ["id", "name", "stock"]


@PG_LIVE
def test_pg_live_health_and_validate(client, pg_live):
    # health：SELECT 1 ok；detail 只含 env:VAR 字面量，不回显 DSN
    resp = client.post(f"/api/v1/connectors/{PG_NAME}/health", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and "SELECT 1 正常" in body["detail"]
    assert body["detail"].count(f"env:{_PG_DSN_ENV}") == 1 and _LIVE_DSN not in body["detail"]
    # validate：status → verified
    resp = client.post(f"/api/v1/connectors/{PG_NAME}/validate", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["status"] == "verified"


@PG_LIVE
def test_pg_live_write_blocked_and_rows_intact(client, pg_live):
    """写语句被白名单拦截（PG 真库上的活体证据：拦截后行数不变，写未触达）。"""
    resp = _invoke(client, T_INS)
    assert resp.status_code == 503 and "EAP-7003" in resp.json()["detail"]
    import psycopg

    conn = psycopg.connect(_LIVE_DSN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM m55_products")
            assert cur.fetchone()[0] == 3  # INSERT 未触达真库
    finally:
        conn.close()

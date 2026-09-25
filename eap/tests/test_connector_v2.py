"""连接器深化测试（M31 任务组 C）：SQL 只读 kind（白名单/命名绑定/行数上限）、OAuth2 凭证托管
（client_credentials + authorization_code + 过期自动刷新，假 transport 全离线）、Health Check
落库与列表透出、TriggerRule target_type=connector 经事件触发全链路、connector.invoked 事件。

HTTP 出站一律注入假 transport（monkeypatch http_client_factory），禁真实网络。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from eap.config import get_settings
from eap.db import SessionLocal
from eap.models import ConnectorRecord
from eap.runtime import connectors as connectors_runtime
from eap.runtime.connectors import load_connector_tools
from eap.runtime.events import bus, emit_event

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

# ---------- M52-D 可重入命名 ----------
# 模块级一次性后缀（module 夹具拿不到 function 级 uname 夹具，样板同 conftest.uname）：
# 连接器/工具名跨运行唯一——脏库重跑既不撞唯一约束（409），也防 resolve_tool 命中
# 上一遍残留的同名工具旧连接器（其 tmp sqlite 文件/已存 token 均已失效，断言必炸）。
_SFX = uuid.uuid4().hex[:8]

DEMO_NAME = f"m31-sql-demo-{_SFX}"
BAD_NAME = f"m31-sql-bad-{_SFX}"
CMT_NAME = f"m31-sql-cmt-{_SFX}"
OAUTH_API_NAME = f"m31-oauth-api-{_SFX}"
OAUTH_CODE_NAME = f"m31-oauth-code-{_SFX}"
DEAD_NAME = f"m31-dead-{_SFX}"
RULE_NAME = f"m31-conn-rule-{_SFX}"
EVENT_TYPE = f"test.connector.m31.{_SFX}"

T_BY_NAME = f"m31-{_SFX}.products.by_name"
T_ALL = f"m31-{_SFX}.products.all"
T_BAD_UPDATE = f"m31-{_SFX}.bad.update"
T_BAD_MULTI = f"m31-{_SFX}.bad.multi"
T_BAD_GLUE = f"m31-{_SFX}.bad.glue"
T_BAD_PRAGMA = f"m31-{_SFX}.bad.pragma"
T_CMT = f"m31-{_SFX}.cmt"
T_FETCH = f"m31-{_SFX}.remote.fetch"
T_FETCH2 = f"m31-{_SFX}.remote.fetch2"
T_DEAD_PING = f"m31-{_SFX}.dead.ping"


# ---------- 假 transport（禁真实网络） ----------

class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self.text = json.dumps(payload or {})


class _FakeClient:
    """token 端点按序出响应并记录 form；业务端点回固定 JSON 并记录调用头。"""

    def __init__(self, token_responses: list[_FakeResponse], api_payload: dict | None = None):
        self.token_responses = list(token_responses)
        self.token_calls: list[dict] = []
        self.api_calls: list[dict] = []
        self.api_payload = api_payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kw):
        self.token_calls.append({"url": url, "data": dict(data or {})})
        if not self.token_responses:
            raise ValueError("假 transport：token 响应已耗尽")
        return self.token_responses.pop(0)

    async def request(self, method, url, params=None, json=None, headers=None):
        self.api_calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        return _FakeResponse(200, self.api_payload)


# ---------- 辅助 ----------

def _invoke(client, tool_name: str, args: dict | None = None):
    """经扩展中心工具试运行端点调用连接器工具（resolve_tool 覆盖连接器工具）。"""
    return client.post(f"/api/v1/extensions/tools/{tool_name}/invoke",
                       headers=HEADERS, json={"args": args or {}})


def _age_token(name: str, seconds: int = 60) -> None:
    """把连接器 access token 置为已过期（触发自动刷新路径）。"""
    with SessionLocal() as db:
        record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
        assert record is not None
        record.oauth_expires_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                                   - timedelta(seconds=seconds))
        db.commit()


def _stored_access_token(name: str) -> str | None:
    from eap.security_crypto import decrypt_secret

    with SessionLocal() as db:
        record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
        return decrypt_secret(record.oauth_access_token_enc) if record else None


def _poll_audit_detail(client, action: str, target: str, want: dict, timeout: float = 15.0) -> dict:
    """轮询审计日志，直到某 action/target 的 detail 覆盖 want（事件触发为异步链路）。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get("/api/v1/audit", headers=AUTH,
                          params={"action": action, "target": target, "limit": 20})
        for entry in resp.json():
            detail = entry.get("detail") or {}
            last = detail
            if all(detail.get(k) == v for k, v in want.items()):
                return detail
        time.sleep(0.2)
    return last


def _poll_queue(q: asyncio.Queue, timeout: float = 15.0):
    """测试线程轮询应用循环投递的事件队列（get_nowait 单操作在 GIL 下足够安全）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return q.get_nowait()
        except asyncio.QueueEmpty:
            time.sleep(0.1)
    return None


# ---------- 夹具：SQL 演示连接器（合法）与非法 SQL 连接器 ----------

@pytest.fixture(scope="module")
def sql_demo(client, tmp_path_factory):
    db_file = tmp_path_factory.mktemp("m31") / "demo.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, stock INTEGER)")
    conn.executemany("INSERT INTO products (name, stock) VALUES (?, ?)",
                     [("EAP 一体机", 15), ("EAP 网关", 42), ("EAP 传感器", 8)])
    conn.commit()
    conn.close()
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": DEMO_NAME, "kind": "sql",
        "description": "M31 SQL 只读演示连接器",
        "config": {"dialect": "sqlite", "database": str(db_file)},
        "endpoints": [
            {"name": "products.by_name", "tool_name": T_BY_NAME,
             "description": "按产品名查询库存",
             "query": "SELECT id, name, stock FROM products WHERE name = :name",
             "params": {"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]}},
            {"name": "products.all", "tool_name": T_ALL,
             "description": "全部产品",
             "query": "SELECT id, name, stock FROM products ORDER BY id"},
        ]})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def sql_bad(client, tmp_path_factory):
    """非法 SQL 连接器：登记成功（登记不校验），执行时白名单拒绝。"""
    db_file = tmp_path_factory.mktemp("m31bad") / "bad.db"
    sqlite3.connect(db_file).close()
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": BAD_NAME, "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(db_file)},
        "endpoints": [
            {"name": "bad.update", "tool_name": T_BAD_UPDATE,
             "query": "UPDATE products SET stock = 0"},
            {"name": "bad.multi", "tool_name": T_BAD_MULTI,
             "query": "SELECT id FROM products; SELECT id FROM products"},
            {"name": "bad.glue", "tool_name": T_BAD_GLUE,
             "query": "xxxSELECT id FROM products"},
            {"name": "bad.pragma", "tool_name": T_BAD_PRAGMA,
             "query": "PRAGMA database_list"},
        ]})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- SQL kind：查询 / 行数上限 / 白名单 ----------

def test_sql_connector_registration_and_tools(client, sql_demo):
    assert sql_demo["kind"] == "sql"
    # 工具清单注入 function-calling Schema
    names = {s["function"]["name"] for s in
             client.get(f"/api/v1/connectors/{DEMO_NAME}/tools", headers=HEADERS).json()}
    assert names == {T_BY_NAME, T_ALL}
    # 登记校验：sql 缺 config.database → 400；端点缺 query → 400
    assert client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": "m31-sql-nocfg", "kind": "sql",
        "endpoints": [{"name": "q1", "tool_name": "m31.q1", "query": "SELECT 1"}]}).status_code == 400
    assert client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": "m31-sql-noq", "kind": "sql",
        "config": {"dialect": "sqlite", "database": "x.db"},
        "endpoints": [{"name": "q2", "tool_name": "m31.q2"}]}).status_code == 400


def test_sql_endpoint_query_returns_rows(client, sql_demo):
    resp = _invoke(client, T_BY_NAME, {"name": "EAP 一体机"})
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert result["columns"] == ["id", "name", "stock"]
    assert result["rows"] == [[1, "EAP 一体机", 15]]
    assert result["truncated"] is False
    # 无参数端点
    resp = _invoke(client, T_ALL)
    assert len(json.loads(resp.json()["output"])["rows"]) == 3
    # 缺命名参数 → 清晰错误
    resp = _invoke(client, T_BY_NAME)
    assert resp.status_code == 503 and "EAP-7000" in resp.json()["detail"]


def test_sql_endpoint_row_limit_truncation(client, sql_demo, monkeypatch):
    monkeypatch.setattr(get_settings(), "connector_sql_max_rows", 2)
    resp = _invoke(client, T_ALL)
    assert resp.status_code == 200, resp.text
    result = json.loads(resp.json()["output"])
    assert len(result["rows"]) == 2
    assert result["truncated"] is True
    assert result["columns"] == ["id", "name", "stock"]


def test_sql_endpoint_rejects_illegal_sql(client, sql_bad):
    # UPDATE → 首词拒绝
    resp = _invoke(client, T_BAD_UPDATE)
    assert resp.status_code == 503 and "EAP-7003" in resp.json()["detail"]
    # 多语句（分号）拒绝
    resp = _invoke(client, T_BAD_MULTI)
    assert resp.status_code == 503 and "多语句" in resp.json()["detail"]
    # 词边界绕过：xxxSELECT 粘词首词拒绝
    resp = _invoke(client, T_BAD_GLUE)
    assert resp.status_code == 503 and "EAP-7003" in resp.json()["detail"]
    # PRAGMA 拒绝
    resp = _invoke(client, T_BAD_PRAGMA)
    assert resp.status_code == 503 and "EAP-7003" in resp.json()["detail"]
    # 注释剥离后合法：-- DROP 在注释里不拦（注释先剥再校验）
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": CMT_NAME, "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
        "endpoints": [{"name": "cmt", "tool_name": T_CMT,
                       "query": "  -- init\nSELECT 1 AS ok -- DROP TABLE x\n"}]})
    assert resp.status_code == 200, resp.text
    resp = _invoke(client, T_CMT)
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["output"])["rows"] == [[1]]


# ---------- OAuth2 凭证托管 ----------

def test_oauth_client_credentials_bearer_and_refresh(client, monkeypatch):
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": OAUTH_API_NAME, "kind": "rest", "base_url": "http://api.internal.local",
        "endpoints": [{"name": "fetch", "tool_name": T_FETCH,
                       "method": "GET", "path": "/data"}],
        "oauth_client_id": "cid-1", "oauth_client_secret": "fake-cs-3cret",
        "oauth_token_url": "https://idp.example/oauth/token", "oauth_scopes": "read write"})
    assert resp.status_code == 200, resp.text

    fake = _FakeClient([
        _FakeResponse(payload={"access_token": "AT1", "refresh_token": "RT1",
                               "token_type": "Bearer", "expires_in": 3600}),
        _FakeResponse(payload={"access_token": "AT2", "refresh_token": "RT2",
                               "token_type": "Bearer", "expires_in": 3600}),
    ], api_payload={"msg": "hi"})
    monkeypatch.setattr(connectors_runtime, "http_client_factory", lambda timeout=10.0: fake)

    # 手动获取（admin）：client_credentials 换 token；响应不回显完整 token
    resp = client.post(f"/api/v1/connectors/{OAUTH_API_NAME}/oauth/token",
                       headers=AUTH, json={"refresh": False})
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["status"] == "ok" and view["has_refresh_token"] is True and view["expires_at"]
    assert "AT1" not in resp.text and "RT1" not in resp.text
    assert fake.token_calls == [{
        "url": "https://idp.example/oauth/token",
        "data": {"grant_type": "client_credentials", "client_id": "cid-1",
                 "client_secret": "fake-cs-3cret", "scope": "read write"}}]

    # 工具调用：有效 token 快路径 → Bearer AT1，不触发新 token 请求
    resp = _invoke(client, T_FETCH)
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["output"]) == {"status": 200, "body": {"msg": "hi"}}
    assert len(fake.token_calls) == 1
    assert fake.api_calls[0]["headers"]["Authorization"] == "Bearer AT1"

    # token 过期 → 工具调用自动 refresh_token 刷新 → Bearer AT2
    _age_token(OAUTH_API_NAME)
    resp = _invoke(client, T_FETCH)
    assert resp.status_code == 200, resp.text
    assert len(fake.token_calls) == 2
    assert fake.token_calls[1]["data"]["grant_type"] == "refresh_token"
    assert fake.token_calls[1]["data"]["refresh_token"] == "RT1"
    assert fake.api_calls[-1]["headers"]["Authorization"] == "Bearer AT2"
    assert _stored_access_token(OAUTH_API_NAME) == "AT2"


def test_oauth_authorization_code_and_state_csrf(client, monkeypatch):
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": OAUTH_CODE_NAME, "kind": "rest", "base_url": "http://api.internal.local",
        "endpoints": [{"name": "fetch", "tool_name": T_FETCH2,
                       "method": "GET", "path": "/data"}],
        "oauth_client_id": "cid-9", "oauth_client_secret": "cs-9",
        "oauth_token_url": "https://idp.example/oauth/token"})
    assert resp.status_code == 200, resp.text

    # 第一步：生成跳转 URL + 一次性 state
    resp = client.get(f"/api/v1/connectors/{OAUTH_CODE_NAME}/oauth/authorize",
                      headers=AUTH, params={"redirect_uri": "http://localhost:8300/cb"})
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert "response_type=code" in view["authorize_url"]
    assert "client_id=cid-9" in view["authorize_url"]
    assert view["state"] in view["authorize_url"]
    assert "redirect_uri=" in view["authorize_url"]

    # 第二步：code 换 token（state 校验 + redirect_uri 回传）
    fake = _FakeClient([_FakeResponse(payload={"access_token": "CODE-AT", "expires_in": 1800})])
    monkeypatch.setattr(connectors_runtime, "http_client_factory", lambda timeout=10.0: fake)
    resp = client.post(f"/api/v1/connectors/{OAUTH_CODE_NAME}/oauth/callback",
                       headers=AUTH, json={"code": "auth-code-1", "state": view["state"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "bound"
    assert fake.token_calls[0]["data"]["grant_type"] == "authorization_code"
    assert fake.token_calls[0]["data"]["code"] == "auth-code-1"
    assert fake.token_calls[0]["data"]["redirect_uri"] == "http://localhost:8300/cb"

    # state 一次性：重放 → 400（CSRF/重放防护）；伪造 state → 400
    assert client.post(f"/api/v1/connectors/{OAUTH_CODE_NAME}/oauth/callback", headers=AUTH,
                       json={"code": "x", "state": view["state"]}).status_code == 400
    assert client.post(f"/api/v1/connectors/{OAUTH_CODE_NAME}/oauth/callback", headers=AUTH,
                       json={"code": "x", "state": "forged"}).status_code == 400

    # 工具调用走已落库 token 快路径 → Bearer CODE-AT
    resp = _invoke(client, T_FETCH2)
    assert resp.status_code == 200, resp.text
    assert fake.api_calls[0]["headers"]["Authorization"] == "Bearer CODE-AT"
    assert len(fake.token_calls) == 1


# ---------- Health Check ----------

def test_health_check_ok_fail_and_list(client, sql_demo):
    # mock-erp 恒 ok
    resp = client.post("/api/v1/connectors/mock-erp/health", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    # sql → SELECT 1 ok
    resp = client.post(f"/api/v1/connectors/{DEMO_NAME}/health", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    # 不可达 rest → fail
    assert client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": DEAD_NAME, "kind": "rest", "base_url": "http://127.0.0.1:9",
        "endpoints": [{"name": "ping", "tool_name": T_DEAD_PING}]}).status_code == 200
    resp = client.post(f"/api/v1/connectors/{DEAD_NAME}/health", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["ok"] is False
    # 结果落库 + 列表 _view 透出
    names = {c["name"]: c for c in client.get("/api/v1/connectors", headers=HEADERS).json()}
    assert names["mock-erp"]["last_health_ok"] is True and names["mock-erp"]["last_health_at"]
    assert names[DEMO_NAME]["last_health_ok"] is True
    assert names[DEAD_NAME]["last_health_ok"] is False
    # 审计 connector.health
    resp = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "connector.health", "target": DEAD_NAME, "limit": 5})
    entries = resp.json()
    assert entries and entries[0]["detail"]["ok"] is False
    # 不存在 → 404
    assert client.post("/api/v1/connectors/m31-nope/health", headers=AUTH).status_code == 404


# ---------- TriggerRule target_type=connector 全链路 ----------

def test_trigger_connector_full_chain(client, sql_demo):
    # target_type=connector 进入校验 pattern
    resp = client.post("/api/v1/triggers", headers=AUTH, json={
        "name": RULE_NAME, "source": "event", "event_type": EVENT_TYPE,
        "target_type": "connector", "target_name": DEMO_NAME})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]
    # 非法 target_type → 422
    assert client.post("/api/v1/triggers", headers=AUTH, json={
        "name": "m31-bad-tt", "source": "event", "event_type": "x.y",
        "target_type": "database", "target_name": "x"}).status_code == 422

    q = bus.subscribe("connector.*")
    try:
        # 发射事件（测试线程 → call_soon_threadsafe 调度到应用循环）
        emit_event(EVENT_TYPE, data={"endpoint": T_BY_NAME,
                                               "arguments": {"name": "EAP 网关"}})
        # trigger.fire 审计：status=ok，ref 含查询结果，detail 标 connector
        detail = _poll_audit_detail(client, "trigger.fire", RULE_NAME, {"status": "ok"})
        assert detail.get("status") == "ok", detail
        assert detail.get("connector") == DEMO_NAME
        assert "EAP 网关" in str(detail.get("ref", "")) and "42" in str(detail.get("ref", ""))
        # 连接器调用路径发射 connector.invoked（应用循环投递）
        event = _poll_queue(q)
        assert event is not None, "应收到 connector.invoked 事件"
        assert event["type"] == "connector.invoked"
        assert event["data"]["connector"] == DEMO_NAME
        assert event["data"]["endpoint"] == T_BY_NAME
        assert event["data"]["status"] == "ok"
    finally:
        bus.unsubscribe(q)
    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200


def test_connector_invoked_event_on_direct_call(client, sql_demo, sql_bad):
    """直调连接器工具 handler：成功/失败均发 connector.invoked（自有事件循环内确定性断言）。"""

    async def scenario():
        q = bus.subscribe("connector.*")
        try:
            with SessionLocal() as db:
                demo = db.scalar(select(ConnectorRecord)
                                 .where(ConnectorRecord.name == DEMO_NAME))
                bad = db.scalar(select(ConnectorRecord)
                                .where(ConnectorRecord.name == BAD_NAME))
                tool = next(t for t in load_connector_tools(demo)
                            if t.name == T_BY_NAME)
                bad_tool = next(t for t in load_connector_tools(bad)
                                if t.name == T_BAD_UPDATE)
            out = await tool.handler(json.dumps({"name": "EAP 传感器"}))
            ok_event = await asyncio.wait_for(q.get(), 5)
            with pytest.raises(ValueError):
                await bad_tool.handler("{}")
            err_event = await asyncio.wait_for(q.get(), 5)
            return json.loads(out), ok_event, err_event
        finally:
            bus.unsubscribe(q)

    result, ok_event, err_event = asyncio.run(scenario())
    assert result["rows"] == [[3, "EAP 传感器", 8]]
    assert ok_event["data"] == {"connector": DEMO_NAME,
                                "endpoint": T_BY_NAME, "status": "ok"}
    assert err_event["data"]["status"] == "error"
    assert err_event["data"]["connector"] == BAD_NAME

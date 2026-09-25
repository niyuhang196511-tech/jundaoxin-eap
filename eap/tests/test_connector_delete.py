"""M53-B 连接器 DELETE 端点测试：硬删除 + 引用语义 + 密钥随行 + 审计。

覆盖：删除成功（列表/详情/tools 消失、工具池不再装载）、二次删除 404、member JWT 403、
审计 connector.delete 落库且 detail 零 secret 值、被触发器规则引用 → 409 EAP-2002 列出引用
（解除引用后可删）、带 api_key/oauth 密文的连接器删除后整行消失。

全部用例脏库可重入：连接器/触发器名带 uuid 后缀唯一，审计查询按唯一 target 过滤，
无全局计数断言——同一库连跑多遍不互相污染（样板 test_connector_patch.py）。
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import ConnectorRecord
from eap.runtime.connectors import load_enabled_connector_tools

from .conftest import AUTH
from .test_jwt_auth import _access_token
from .test_oidc import fake_idp  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}


# ---------- 辅助（全部 uuid 唯一名，脏库可重入） ----------

def _unique(prefix: str = "m53b") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_rest(client: TestClient, **overrides) -> str:
    """造 rest 连接器（登记不触网，离线确定），返回唯一名。工具名 = <name>.ep0。"""
    name = overrides.pop("name", None) or _unique()
    payload: dict = {"name": name, "kind": "rest",
                     "base_url": "http://erp.internal.local/api",
                     "description": "M53-B 删除流测试连接器",
                     "endpoints": [{"name": "ep0", "tool_name": f"{name}.ep0",
                                    "method": "GET", "path": "/ep0"}]}
    payload.update(overrides)
    resp = client.post("/api/v1/connectors", headers=HEADERS, json=payload)
    assert resp.status_code == 200, resp.text
    return name


def _record(name: str) -> ConnectorRecord | None:
    with SessionLocal() as db:
        return db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))


def _pool_tool_names() -> set[str]:
    """当前启用连接器的工具池全名集合（load_enabled_connector_tools 实时读库）。"""
    with SessionLocal() as db:
        return {t.name for t in load_enabled_connector_tools(db)}


def _make_trigger(client: TestClient, connector_name: str) -> int:
    """建 target_type=connector 的触发规则指向该连接器，返回 rule id。"""
    rule_name = _unique("m53b-rule")
    resp = client.post("/api/v1/triggers", headers=HEADERS, json={
        "name": rule_name, "source": "event", "event_type": f"test.m53b.{rule_name}",
        "target_type": "connector", "target_name": connector_name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ---------- a) 删除成功：列表/详情/tools 消失、工具池不再装载 ----------

def test_delete_success_purges_everywhere(client: TestClient):
    name = _create_rest(client)
    tool = f"{name}.ep0"
    assert tool in _pool_tool_names(), "删除前工具应在池中"

    resp = client.delete(f"/api/v1/connectors/{name}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"name": name, "status": "deleted"}

    # 列表不再含该连接器
    names = {c["name"] for c in client.get("/api/v1/connectors", headers=AUTH).json()}
    assert name not in names
    # 详情/tools → 404
    assert client.get(f"/api/v1/connectors/{name}", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/connectors/{name}/tools", headers=AUTH).status_code == 404
    # 工具池不再装载
    assert tool not in _pool_tool_names()
    # 整行落库消失
    assert _record(name) is None


# ---------- b) 二次删除 404 ----------

def test_delete_twice_404(client: TestClient):
    name = _create_rest(client)
    assert client.delete(f"/api/v1/connectors/{name}", headers=HEADERS).status_code == 200
    resp = client.delete(f"/api/v1/connectors/{name}", headers=HEADERS)
    assert resp.status_code == 404 and "EAP-4004" in resp.json()["detail"]


def test_delete_missing_404(client: TestClient):
    missing = _unique("m53b-nope")
    resp = client.delete(f"/api/v1/connectors/{missing}", headers=HEADERS)
    assert resp.status_code == 404 and "EAP-4004" in resp.json()["detail"]


# ---------- c) member JWT 403 ----------

def test_delete_requires_admin(client: TestClient, fake_idp):  # noqa: F811  fixture 再导出
    name = _create_rest(client)
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    resp = client.delete(f"/api/v1/connectors/{name}", headers=member)
    assert resp.status_code == 403
    # member 删除被拒 → 记录仍在
    assert _record(name) is not None


# ---------- d) 审计 connector.delete 落库且 detail 无 secret ----------

def test_delete_audit_no_secret_values(client: TestClient):
    secret_key = "fake-key-m53b-audit"
    secret_oauth = "fake-oauth-m53b-secret"
    name = _create_rest(client, api_key=secret_key,
                        oauth_client_id="cid-m53b", oauth_client_secret=secret_oauth,
                        oauth_token_url="https://idp.example/oauth/token")
    assert client.delete(f"/api/v1/connectors/{name}", headers=HEADERS).status_code == 200

    entries = client.get("/api/v1/audit", headers=AUTH,
                         params={"action": "connector.delete", "target": name,
                                 "limit": 20}).json()
    assert entries, "应有 connector.delete 审计行"
    detail = entries[0]["detail"]
    # 处置结果只记存在性布尔/计数
    assert detail["kind"] == "rest"
    assert detail["endpoint_count"] == 1
    assert detail["had_api_key"] is True
    assert detail["had_oauth_secret"] is True
    assert detail["trigger_refs"] == 0
    # detail 序列化绝不含任何 secret 明文/密文
    serialized = json.dumps(detail, ensure_ascii=False)
    assert secret_key not in serialized
    assert secret_oauth not in serialized


# ---------- e) 引用语义：被触发器规则引用 → 409，解除引用后可删 ----------

def test_delete_blocked_by_trigger_reference(client: TestClient):
    name = _create_rest(client)
    rid = _make_trigger(client, name)

    resp = client.delete(f"/api/v1/connectors/{name}", headers=HEADERS)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "EAP-2002" in detail and "触发器" in detail
    # 记录仍在（未被误删）
    assert _record(name) is not None

    # 停用规则仍算引用（停用可再启用，留悬空即埋雷）→ 仍 409
    assert client.patch(f"/api/v1/triggers/{rid}", headers=HEADERS,
                        json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/v1/connectors/{name}", headers=HEADERS).status_code == 409

    # 删除引用规则后可删
    assert client.delete(f"/api/v1/triggers/{rid}", headers=HEADERS).status_code == 200
    assert client.delete(f"/api/v1/connectors/{name}", headers=HEADERS).status_code == 200
    assert _record(name) is None


# ---------- f) 带 api_key/oauth 密文的连接器删除：整行随行消失 ----------

def test_delete_purges_secret_columns(client: TestClient):
    name = _create_rest(client, api_key="fake-key-m53b-purge",
                        oauth_client_id="cid-m53b-purge",
                        oauth_client_secret="fake-oauth-m53b-purge",
                        oauth_token_url="https://idp.example/oauth/token")
    rec = _record(name)
    assert rec is not None
    rec_id = rec.id
    assert rec.api_key is not None and rec.oauth_client_secret_enc is not None

    assert client.delete(f"/api/v1/connectors/{name}", headers=HEADERS).status_code == 200
    # 整行删除 → 密文列随行消失（无子表，无残留）
    with SessionLocal() as db:
        assert db.get(ConnectorRecord, rec_id) is None
    assert _record(name) is None

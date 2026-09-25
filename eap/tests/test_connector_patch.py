"""M52-C 连接器 PATCH 编辑流测试：GET /{name} 详情回填（secret 一律不回显）+ PATCH 局部更新。

覆盖：基础字段更新落库、连接目标变更 status 重置 pending、endpoints 整体替换（工具池
实时反映）、secret 三态（缺省=保留 / ""=清除 / 非空=加密覆盖）、name/kind 不可变、
404、member JWT 403、审计 connector.update 只记字段名不记 secret 值、合并后逐 kind
校验（EAP-7002/EAP-7003）。

全部用例脏库可重入：连接器名带 uuid 后缀唯一，审计查询按唯一 target 过滤，
无全局计数断言——同一库连跑多遍不互相污染。
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import ConnectorRecord
from eap.security_crypto import decrypt_secret

from .conftest import AUTH
from .test_jwt_auth import _access_token
from .test_oidc import fake_idp  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}


# ---------- 辅助（全部 uuid 唯一名，脏库可重入） ----------

def _unique(prefix: str = "m52c") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _rest_endpoints(name: str) -> list[dict]:
    return [{"name": "ep0", "tool_name": f"{name}.ep0", "method": "GET", "path": "/ep0"}]


def _create_rest(client: TestClient, **overrides) -> str:
    """造 rest 连接器（登记不触网，离线确定），返回唯一名。"""
    name = overrides.pop("name", None) or _unique()
    payload: dict = {"name": name, "kind": "rest",
                     "base_url": "http://erp.internal.local/api",
                     "description": "M52-C 编辑流测试连接器",
                     "endpoints": _rest_endpoints(name)}
    payload.update(overrides)
    resp = client.post("/api/v1/connectors", headers=HEADERS, json=payload)
    assert resp.status_code == 200, resp.text
    return name


def _create_sql(client: TestClient) -> str:
    name = _unique()
    resp = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": name, "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
        "endpoints": [{"name": "q1", "tool_name": f"{name}.q1", "query": "SELECT 1"}]})
    assert resp.status_code == 200, resp.text
    return name


def _record(name: str) -> ConnectorRecord | None:
    with SessionLocal() as db:
        return db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))


def _set_status(name: str, status: str) -> None:
    with SessionLocal() as db:
        rec = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
        assert rec is not None
        rec.status = status
        db.commit()


# ---------- a) 基础字段更新 ----------

def test_patch_updates_basic_fields(client: TestClient):
    """description/base_url/header_name/enabled 更新 → 200 且落库、视图反映。"""
    name = _create_rest(client)
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={
        "description": "M52-C 更新后的描述",
        "base_url": "http://erp2.internal.local/api",
        "header_name": "X-Api-Key",
        "enabled": False})
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["description"] == "M52-C 更新后的描述"
    assert view["base_url"] == "http://erp2.internal.local/api"
    assert view["enabled"] is False
    # 落库
    rec = _record(name)
    assert rec is not None
    assert rec.description == "M52-C 更新后的描述"
    assert rec.header_name == "X-Api-Key" and rec.enabled is False
    # 详情视图反映
    detail = client.get(f"/api/v1/connectors/{name}", headers=AUTH).json()
    assert detail["description"] == "M52-C 更新后的描述"
    assert detail["header_name"] == "X-Api-Key" and detail["enabled"] is False


def test_patch_updates_sql_config(client: TestClient):
    """sql 连接器 config 更新 → 落库；config 属连接目标 → status 重置 pending。"""
    name = _create_sql(client)
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"config": {"dialect": "sqlite", "database": "other.db"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    detail = client.get(f"/api/v1/connectors/{name}", headers=AUTH).json()
    assert detail["config"] == {"dialect": "sqlite", "database": "other.db"}


# ---------- b) status 重置 ----------

def test_patch_base_url_resets_status(client: TestClient):
    """base_url 变更 → status 重置 pending（须重新 validate）；无关字段/同值不重置。"""
    name = _create_rest(client)
    _set_status(name, "verified")
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"base_url": "http://moved.internal.local/api"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"

    # 与连接目标无关的字段不触发重置
    _set_status(name, "verified")
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"description": "只改描述"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified"

    # 显式提交与库存相同的 base_url 不算变更
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"base_url": "http://moved.internal.local/api"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified"


# ---------- c) endpoints 整体替换 ----------

def test_patch_endpoints_replaces_tools(client: TestClient):
    """endpoints 整体替换 → GET /{name}/tools 实时反映新工具（load_connector_tools 实时读库）。"""
    name = _create_rest(client)
    new_eps = [
        {"name": "alpha", "tool_name": f"{name}.alpha", "method": "POST", "path": "/a",
         "requires_approval": True},
        {"name": "beta", "tool_name": f"{name}.beta", "method": "GET", "path": "/b"},
    ]
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"endpoints": new_eps})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"  # endpoints 属连接目标 → 重置
    tools = client.get(f"/api/v1/connectors/{name}/tools", headers=AUTH).json()
    assert {t["function"]["name"] for t in tools} == {f"{name}.alpha", f"{name}.beta"}
    # 详情视图 = 全对象数组；库存 endpoints JSON 一致
    detail = client.get(f"/api/v1/connectors/{name}", headers=AUTH).json()
    assert [e["tool_name"] for e in detail["endpoints"]] == [f"{name}.alpha", f"{name}.beta"]
    assert detail["endpoints"][0]["requires_approval"] is True
    rec = _record(name)
    assert [e["tool_name"] for e in rec.endpoints] == [f"{name}.alpha", f"{name}.beta"]


# ---------- d) secret 三态 ----------

def test_patch_api_key_tri_state(client: TestClient):
    """api_key：缺省=保留；非空=encrypt_secret 覆盖（密文落库）；显式 ""=清除（置 None）。"""
    name = _create_rest(client, api_key="fake-key-m52c-old")
    resp = client.get(f"/api/v1/connectors/{name}", headers=AUTH)
    detail = resp.json()
    assert detail["has_api_key"] is True
    # secret 一律不回显：响应无明文，也无 api_key / 密文字段本身
    assert "fake-key-m52c-old" not in resp.text
    assert "api_key" not in detail

    # 缺省 = 保留原值
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"description": "不动密钥"})
    assert resp.status_code == 200, resp.text
    rec = _record(name)
    assert decrypt_secret(rec.api_key) == "fake-key-m52c-old"

    # 非空 = 覆盖（经 encrypt_secret 落库；未配 EAP_SECRET_KEY 时为明文兼容形态原样存，
    # 密文形态由 security_crypto 自身测试覆盖——此处断言解密回读值正确即可）
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"api_key": "fake-key-m52c-new"})
    assert resp.status_code == 200, resp.text
    rec = _record(name)
    assert decrypt_secret(rec.api_key) == "fake-key-m52c-new"

    # 显式 "" = 清除
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"api_key": ""})
    assert resp.status_code == 200, resp.text
    rec = _record(name)
    assert rec.api_key is None
    assert client.get(f"/api/v1/connectors/{name}", headers=AUTH).json()["has_api_key"] is False


def test_patch_oauth_secret_tri_state_and_plaintext_fields(client: TestClient):
    """oauth_client_secret 三态同 api_key；client_id/token_url/scopes 明文回显（对齐 create 入参）。"""
    name = _create_rest(client,
                        oauth_client_id="cid-m52c", oauth_client_secret="fake-oauth-m52c-secret",
                        oauth_token_url="https://idp.example/oauth/token",
                        oauth_scopes="read write")
    resp = client.get(f"/api/v1/connectors/{name}", headers=AUTH)
    detail = resp.json()
    assert detail["oauth_client_id"] == "cid-m52c"
    assert detail["oauth_token_url"] == "https://idp.example/oauth/token"
    assert detail["oauth_scopes"] == "read write"
    assert detail["has_oauth_secret"] is True
    assert "fake-oauth-m52c-secret" not in resp.text
    assert "oauth_client_secret_enc" not in detail

    # 缺省 = 保留
    client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"description": "不动 oauth"})
    rec = _record(name)
    assert decrypt_secret(rec.oauth_client_secret_enc) == "fake-oauth-m52c-secret"
    # 非空 = 加密覆盖（同批更新明文字段）
    client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                 json={"oauth_client_secret": "fake-oauth-m52c-new", "oauth_client_id": "cid-m52c-2"})
    rec = _record(name)
    assert decrypt_secret(rec.oauth_client_secret_enc) == "fake-oauth-m52c-new"
    assert rec.oauth_client_id == "cid-m52c-2"
    # "" = 清除
    client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={"oauth_client_secret": ""})
    rec = _record(name)
    assert rec.oauth_client_secret_enc is None
    assert client.get(f"/api/v1/connectors/{name}", headers=AUTH).json()["has_oauth_secret"] is False


def test_patch_ignores_name_and_kind(client: TestClient):
    """name/kind 不可变：PATCH 多传这两个字段被忽略（pydantic 默认），记录不变。"""
    name = _create_rest(client)
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"name": "hacked-name", "kind": "sql", "description": "尝试改不可变字段"})
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["name"] == name and view["kind"] == "rest"
    assert view["description"] == "尝试改不可变字段"


# ---------- e) 404 ----------

def test_patch_and_detail_missing_404(client: TestClient):
    missing = _unique("m52c-nope")
    assert client.get(f"/api/v1/connectors/{missing}", headers=AUTH).status_code == 404
    resp = client.patch(f"/api/v1/connectors/{missing}", headers=HEADERS, json={"description": "x"})
    assert resp.status_code == 404 and "EAP-4004" in resp.json()["detail"]


# ---------- f) RBAC ----------

def test_patch_and_detail_require_admin(client: TestClient, fake_idp):  # noqa: F811  fixture 再导出
    """member JWT → 403（require_admin，对齐 test_audit_governance 的模式）。"""
    name = _create_rest(client)
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}",
              "Content-Type": "application/json"}
    assert client.patch(f"/api/v1/connectors/{name}", headers=member,
                        json={"description": "member 不可改"}).status_code == 403
    assert client.get(f"/api/v1/connectors/{name}", headers=member).status_code == 403


# ---------- g) 审计 ----------

def test_patch_audit_records_field_names_only(client: TestClient):
    """审计 connector.update：detail.fields = 提交字段名；detail 序列化不含任何 secret 明文。"""
    name = _create_rest(client)
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS, json={
        "description": "审计断言", "api_key": "fake-key-m52c-audit"})
    assert resp.status_code == 200, resp.text
    entries = client.get("/api/v1/audit", headers=AUTH,
                         params={"action": "connector.update", "target": name,
                                 "limit": 20}).json()
    assert entries, "应有 connector.update 审计行"
    detail = entries[0]["detail"]
    assert sorted(detail["fields"]) == ["api_key", "description"]
    assert detail["has_oauth"] is False
    # detail 只记字段名，绝不记 secret 值
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "fake-key-m52c-audit" not in serialized


# ---------- h) 合并后逐 kind 校验 ----------

def test_patch_validates_merged_values(client: TestClient):
    """校验用合并后的最终值：rest 非 http(s) → EAP-7002；sql 缺 database/query → EAP-7003。"""
    # rest：base_url 改成非 http(s) → 400 EAP-7002，且被拒更新不落库
    name = _create_rest(client)
    resp = client.patch(f"/api/v1/connectors/{name}", headers=HEADERS,
                        json={"base_url": "ftp://bad.internal.local/x"})
    assert resp.status_code == 400 and "EAP-7002" in resp.json()["detail"]
    rec = _record(name)
    assert rec.base_url == "http://erp.internal.local/api"

    # sql：patch 掉 config.database → 400 EAP-7003
    sql_name = _create_sql(client)
    resp = client.patch(f"/api/v1/connectors/{sql_name}", headers=HEADERS,
                        json={"config": {"dialect": "sqlite"}})
    assert resp.status_code == 400 and "EAP-7003" in resp.json()["detail"]
    # sql：patch 端点缺 query → 400 EAP-7003
    resp = client.patch(f"/api/v1/connectors/{sql_name}", headers=HEADERS,
                        json={"endpoints": [{"name": "noq", "tool_name": f"{sql_name}.noq"}]})
    assert resp.status_code == 400 and "EAP-7003" in resp.json()["detail"]
    # 无关字段更新：合并值仍合法 → 200，且非连接目标字段不重置 status
    resp = client.patch(f"/api/v1/connectors/{sql_name}", headers=HEADERS,
                        json={"description": "sql 未动连接目标"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] != "pending"

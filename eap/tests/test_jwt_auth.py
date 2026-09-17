"""M6 资源服务器模式：外部 IdP JWT（access token）→ 租户映射 → 三轨认证。"""

from __future__ import annotations

import time

from .conftest import AUTH
from .test_oidc import _make_id_token, fake_idp  # noqa: F401,F811 复用模拟 IdP 夹具（fixture 再导出）

ISSUER = "https://idp.example"


def _access_token(**overrides) -> str:
    claims = {"iss": ISSUER, "sub": "alice@corp", "exp": int(time.time()) + 600,
              "tenant_id": 1, "roles": ["admin"]}
    claims.update(overrides)
    return _make_id_token(claims)


def test_jwt_auth_maps_tenant(client, fake_idp):
    """JWT 三轨：验签通过 → tenant_id 声明映射租户 → 调用成功并带用户身份。"""
    token = _access_token()
    r = client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text  # require_api_key 允许 jwt 通道


def test_jwt_auth_bad_signature_rejected(client, fake_idp):
    """篡改 payload → 验签失败 → 401。"""
    from .test_oidc import _b64url

    token = _access_token()
    forged_payload = _b64url(b'{"iss": "https://idp.example", "sub": "attacker", "exp": 9999999999}')
    parts = token.split(".")
    token = f"{parts[0]}.{forged_payload}.{parts[2]}"
    r = client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_auth_unknown_tenant_rejected(client, fake_idp):
    """tenant_id 声明映射不到租户 → 403。"""
    token = _access_token(tenant_id=9999)
    r = client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_jwt_auth_expired_rejected(client, fake_idp):
    token = _access_token(exp=int(time.time()) - 10)
    r = client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_api_key_still_works_with_oidc_configured(client, fake_idp):
    """OIDC 配置后 API Key 通道不受影响（三轨共存）。"""
    r = client.get("/api/v1/conversations", headers=AUTH)
    assert r.status_code == 200


def test_rbac_member_blocked_admin_only(client, fake_idp):
    """M14 RBAC：member 角色可读但 admin 写操作 403；admin 角色（含服务间 API Key）放行。"""
    member = _access_token(roles=["member"])
    admin = _access_token(roles=["admin"])

    # member 读 OK
    assert client.get("/api/v1/models", headers={"Authorization": f"Bearer {member}"}).status_code == 200
    # member 写模型 → 403（require_admin）
    resp = client.post("/api/v1/models", headers={"Authorization": f"Bearer {member}"}, json={
        "name": "rbac-test-model", "capabilities": ["chat"]})
    assert resp.status_code == 403, resp.text
    # member 审计查询 → 403
    assert client.get("/api/v1/audit", headers={"Authorization": f"Bearer {member}"}).status_code == 403
    # admin 写 OK
    resp = client.post("/api/v1/models", headers={"Authorization": f"Bearer {admin}"}, json={
        "name": "rbac-test-model", "capabilities": ["chat"]})
    assert resp.status_code == 200, resp.text
    # 服务间 API Key 写 OK（运维全量语义）
    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": "rbac-test-model-2", "capabilities": ["chat"]})
    assert resp.status_code == 200, resp.text

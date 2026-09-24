"""租户目录 API（M50-B1）：GET /api/v1/tenants（admin only，控制台租户下拉数据源）。

形状断言对齐 Tenant 表实际字段（id/name/created_at，models/__init__.py）；
RBAC 对齐 budgets 导出 / audit 查询：API Key 与 admin JWT 200，member JWT 403。
"""

from __future__ import annotations

import time

from .conftest import AUTH
from .test_oidc import fake_idp, _make_id_token  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

ISSUER = "https://idp.example"


def _token(roles: list[str]) -> str:
    claims = {"iss": ISSUER, "sub": "tenants@corp", "exp": int(time.time()) + 600,
              "tenant_id": 1, "roles": roles}
    return _make_id_token(claims)


def test_list_tenants_shape(client):
    """列表形状：JSON 数组，元素恰含 Tenant 实际字段 id/name/created_at；种子租户可见。"""
    resp = client.get("/api/v1/tenants", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list) and data, "种子库至少含默认租户（seed.py dev_tenant）"
    for row in data:
        assert set(row.keys()) == {"id", "name", "created_at"}
        assert isinstance(row["id"], int) and isinstance(row["name"], str)
    ids = [t["id"] for t in data]
    assert ids == sorted(ids), "id 升序"


def test_list_tenants_reflects_new_row(client):
    """新插入的租户行出现在列表（id/name 回显真实列值，不虚构字段）。"""
    from eap.db import SessionLocal
    from eap.models import Tenant

    name = f"m50b1-tenant-{int(time.time() * 1000)}"
    with SessionLocal() as db:
        row = Tenant(name=name)
        db.add(row)
        db.commit()
        new_id = row.id
    try:
        data = client.get("/api/v1/tenants", headers=AUTH).json()
        assert any(t["id"] == new_id and t["name"] == name for t in data)
    finally:
        with SessionLocal() as db:
            db.delete(db.get(Tenant, new_id))
            db.commit()


def test_list_tenants_member_403(client, fake_idp):  # noqa: F811
    """admin 专用：member JWT → 403（EAP-3005）；admin JWT → 200；缺凭证 → 401。"""
    member = _token(roles=["member"])
    resp = client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {member}"})
    assert resp.status_code == 403, resp.text
    assert "EAP-3005" in resp.json()["detail"]

    admin = _token(roles=["admin"])
    resp = client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {admin}"})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)

    assert client.get("/api/v1/tenants").status_code == 401


def test_list_tenants_empty_shape():
    """空库形状：无租户行时返回 []（JSON 数组而非 null/对象）。

    共享测试库恒有种子租户，且 AUTH 凭证的 resolve_tenant 依赖其所属租户存在
    （删除后请求先 401），API 级空库不可达——故用独立内存空库直调视图函数，
    验证序列化形状。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from eap.api.v1.tenants import list_tenants
    from eap.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert list_tenants(db=db) == []

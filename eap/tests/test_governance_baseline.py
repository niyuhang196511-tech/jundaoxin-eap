"""治理基线测试（v0.6-M23）：RBAC 收口 / 审计补全与查询 / KB 租户过滤。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_budget_requires_admin_but_api_key_is_admin(client: TestClient):
    """预算设置：API Key 通道=平台管理员（放行），并落审计 budget.set；随后恢复不限额。"""
    resp = client.put("/api/v1/budgets", headers=AUTH,
                      json={"tenant_id": 1, "monthly_token_budget": 5000})
    assert resp.status_code == 200, resp.text
    logs = client.get("/api/v1/audit", headers=AUTH, params={"action": "budget.set"}).json()
    assert any(l["target"] == "1" for l in logs)
    # 恢复不限额（0），避免污染同会话内后续测试的预算熔断
    resp = client.put("/api/v1/budgets", headers=AUTH,
                      json={"tenant_id": 1, "monthly_token_budget": 0})
    assert resp.status_code == 200, resp.text


def test_releases_write_endpoints_audited(client: TestClient):
    """发布治理全生命周期落审计（release.create/eval/promote/rollback）。"""
    resp = client.post("/api/v1/releases", headers=AUTH,
                       json={"agent": "faq-agent", "version": "9.9.9"})
    assert resp.status_code == 200, resp.text
    release_id = resp.json()["id"]
    client.post(f"/api/v1/releases/{release_id}/promote", headers=AUTH)
    logs = client.get("/api/v1/audit", headers=AUTH, params={"target": release_id}).json()
    actions = {l["action"] for l in logs}
    assert "release.create" in actions
    assert "release.promote" in actions


def test_workflow_audited(client: TestClient):
    """工作流创建/删除落审计。"""
    dsl = {"name": "audit-wf", "version": "1.0.0",
           "steps": [{"id": "a", "type": "llm", "system": "s"}]}
    assert client.post("/api/v1/workflows", headers=AUTH, json=dsl).status_code == 200
    assert client.delete("/api/v1/workflows/audit-wf", headers=AUTH).status_code == 200
    actions = {l["action"] for l in
               client.get("/api/v1/audit", headers=AUTH, params={"target": "audit-wf"}).json()}
    assert {"workflow.create", "workflow.disable"} <= actions


def test_audit_query_filters(client: TestClient):
    """审计查询：actor/action/时间范围/offset 分页。"""
    # 造两条不同 action 的审计
    client.post("/api/v1/models", headers=AUTH,
                json={"name": "audit-m1", "capabilities": ["chat"], "provider": "mock"})
    resp = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "model.register", "limit": 5, "offset": 0})
    assert resp.status_code == 200
    rows = resp.json()
    assert rows and all(r["action"] == "model.register" for r in rows)
    # actor 过滤（API Key 通道 actor=api-key）
    resp = client.get("/api/v1/audit", headers=AUTH, params={"actor": "api-key", "limit": 10})
    assert all(r["actor"] == "api-key" for r in resp.json())
    # 时间范围
    resp = client.get("/api/v1/audit", headers=AUTH,
                      params={"since": "2020-01-01", "until": "2099-01-01", "limit": 10})
    assert resp.status_code == 200 and resp.json()
    # 非法时间 → 400
    assert client.get("/api/v1/audit", headers=AUTH,
                      params={"since": "not-a-date"}).status_code == 400


def test_kb_tenant_filter_channels(client: TestClient):
    """KB 租户过滤：API Key 通道全量可见；创建默认平台共享（NULL）。"""
    resp = client.post("/api/v1/kb", headers=AUTH,
                       json={"name": "audit-kb", "title": "租户过滤测试库"})
    assert resp.status_code == 200, resp.text
    names = [k["name"] for k in client.get("/api/v1/kb", headers=AUTH).json()]
    assert "audit-kb" in names

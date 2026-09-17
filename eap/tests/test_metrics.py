"""M7 可观测性测试：/metrics exposition + 计数器语义。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def _counter(body: str, series: str) -> float:
    for line in body.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_exposition_format(client: TestClient):
    """/metrics 输出 Prometheus 格式；请求与 agent 调用计入计数器。"""
    client.get("/api/v1/models", headers=AUTH)
    client.get("/health")

    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# TYPE eap_requests_total counter" in body
    assert 'eap_requests_total{method="GET",path="/api/v1/models",status="200"}' in body
    assert "# TYPE eap_agent_invocations_total counter" in body


def test_metrics_counters_increment(client: TestClient):
    """同一指标随请求累加。"""
    series = 'eap_requests_total{method="GET",path="/health",status="200"}'
    before = _counter(client.get("/metrics").text, series)
    client.get("/health")
    client.get("/health")
    after = _counter(client.get("/metrics").text, series)
    assert after >= before + 2


def test_audit_log_records_admin_ops(client: TestClient):
    """M11 审计：策略创建/启停、任务审批落审计并可查询；敏感字段脱敏。"""
    # 策略创建（含敏感形态 config 验证脱敏）
    r = client.post("/api/v1/policies", headers=AUTH, json={
        "name": "audit-test-policy", "tenant_id": 0, "kind": "model-allowlist",
        "config": {"models": ["mock-llm"], "api_key": "should-be-masked"},
    })
    assert r.status_code == 200, r.text
    r2 = client.post("/api/v1/policies/audit-test-policy/enabled?enabled=false", headers=AUTH)
    assert r2.status_code == 200

    logs = client.get("/api/v1/audit", headers=AUTH).json()
    actions = [l["action"] for l in logs]
    assert "policy.create" in actions and "policy.toggle" in actions
    create_log = next(l for l in logs if l["action"] == "policy.create")
    assert create_log["detail"]["config"]["api_key"] == "***"

    # 按 action 过滤
    filtered = client.get("/api/v1/audit", headers=AUTH, params={"action": "policy.toggle"}).json()
    assert all(l["action"] == "policy.toggle" for l in filtered)

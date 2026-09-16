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

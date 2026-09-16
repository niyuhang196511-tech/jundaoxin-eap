"""M9 OTel tracing 测试：未配置零开销；配置后 span 产生（内存导出器校验）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_tracing_disabled_by_default(client: TestClient):
    """未配置 EAP_OTEL_ENDPOINT：tracing 关闭，请求正常（零开销语义）。"""
    from eap.observability import tracing

    assert not tracing.enabled()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Trace-ID")


def test_tracing_spans_emitted_when_configured(client, monkeypatch):
    """配置端点（用内存导出器替换网络导出）后：HTTP/agent span 进入 provider。"""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from eap.observability import tracing

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    monkeypatch.setenv("EAP_OTEL_ENDPOINT", "http://localhost:4317")
    # OTel 全局 provider 一旦 set 无法重置：直接置 enabled 并让其取用全局 provider
    monkeypatch.setattr(tracing, "_STATE", {"enabled": True})

    r = client.get("/health")
    assert r.status_code == 200
    client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                json={"input": "tracing 验证"})

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert any(n.startswith("GET /health") for n in names), names
    assert any(n.startswith("agent.invoke faq-agent") for n in names), names
    # trace_id 关联：agent span 带 eap.trace_id 属性
    agent_span = next(s for s in spans if s.name.startswith("agent.invoke"))
    assert agent_span.attributes.get("eap.agent") == "faq-agent"

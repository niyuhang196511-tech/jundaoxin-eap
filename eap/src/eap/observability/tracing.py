"""OTel tracing 导出（docs/08 §1 的 M3 落地）：可选启用，未配置零开销。

- 配置 EAP_OTEL_ENDPOINT（OTLP gRPC/HTTP 端点，如 http://otel-collector:4317）即启用
- 采样：EAP_OTEL_SAMPLE_RATIO（0~1，默认 1.0 全量；生产建议 0.1）
- span 级别：HTTP 请求（TraceMiddleware 自动）+ agent 调用 + 任务执行
- trace_id 与既有 X-Trace-ID 双向兼容：无上游 trace 时以 OTel trace_id 回填响应头
"""

from __future__ import annotations

import logging

from ..config import get_settings

logger = logging.getLogger("eap.otel")

_STATE: dict = {"enabled": False}


def setup() -> None:
    """应用启动时调用：配置 TracerProvider + OTLP 导出器 + FastAPI 插桩。"""
    s = get_settings()
    if not s.otel_endpoint or _STATE["enabled"]:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({
            "service.name": "eap",
            "service.version": _version(),
        }))
        ratio = max(0.0, min(1.0, s.otel_sample_ratio))
        if ratio < 1.0:
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

            provider.sampler = ParentBased(TraceIdRatioBased(ratio))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=s.otel_endpoint)))
        trace.set_tracer_provider(provider)
        _STATE["enabled"] = True
        logger.info("OTel tracing 已启用 → %s（采样 %.2f）", s.otel_endpoint, ratio)
    except Exception as e:  # 观测失败不阻断启动
        logger.warning("OTel 初始化失败（导出器缺失或端点不可达）: %s", e)


def _version() -> str:
    from .. import __version__

    return __version__


def enabled() -> bool:
    return _STATE["enabled"]


def tracer():
    """获取 eap 命名空间的 tracer（未启用时返回 no-op，调用方零分支）。"""
    from opentelemetry import trace

    return trace.get_tracer("eap")

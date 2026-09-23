"""横切·观测：trace_id 中间件 + usage 计量入库 + 进程内 metrics（docs/08 §1、§4）。"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..db import SessionLocal
from ..models import UsageRecord

logger = logging.getLogger("eap.access")


class TraceMiddleware(BaseHTTPMiddleware):
    """全链路 trace_id：入口生成/透传，响应头返回；metrics 计数 + OTel span（可选启用）。

    trace_id 兼容：上游带 X-Request-ID 时作为 OTel span 属性透传；OTel 生成的新 trace_id
    回填到 X-Trace-ID 响应头（两边可互相检索）。
    """

    async def dispatch(self, request: Request, call_next):
        from . import tracing
        from .metrics import incr

        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        request.state.t0 = time.monotonic()

        span_cm = None
        span = None
        if tracing.enabled():
            from opentelemetry.trace import SpanKind

            span_cm = tracing.tracer().start_as_current_span(
                f"{request.method} {request.url.path}", kind=SpanKind.SERVER)
            span = span_cm.__enter__()
            if span is not None:
                span.set_attribute("http.method", request.method)
                span.set_attribute("http.route", request.url.path)
                span.set_attribute("eap.trace_id", trace_id)

        response = None
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            if span is not None:
                span.set_attribute("http.status_code", response.status_code)
        except Exception as e:
            if span is not None:
                span.record_exception(e)
                span.set_attribute("http.status_code", 500)
                span_cm.__exit__(type(e), e, e.__traceback__)
            raise
        try:
            route = request.scope.get("route")
            path_tpl = getattr(route, "path", request.url.path)
            incr("eap_requests_total", {"method": request.method, "path": path_tpl,
                                        "status": str(response.status_code)})
            # 延迟打点已迁至 RequestLatencyMiddleware（M44-C histogram，observability/latency.py）：
            # legacy eap_request_latency_seconds_sum 计数器并入 histogram 的 _sum，不再在此累计
            logger.info("%s %s -> %s [%s]", request.method, path_tpl,
                        response.status_code, trace_id[:8])
        except Exception:
            pass  # 观测不阻断主流程
        if span_cm is not None:
            span_cm.__exit__(None, None, None)
        return response


def record_usage(trace_id: str, tenant_id: int, *, kind: str, model: str,
                 tokens_in: int, tokens_out: int, latency_ms: int,
                 agent: str = "") -> None:
    """计量事实入库（成本中心数据源）+ token 计数器。失败不影响主流程。

    v0.6：按模型定价计得 cost（模型未配价格则 0），支持 agent 归属。
    """
    from .metrics import incr

    cost = _cost_of(model, tokens_in, tokens_out)
    try:
        with SessionLocal() as db:
            db.add(UsageRecord(
                trace_id=trace_id, tenant_id=tenant_id, kind=kind, model=model,
                agent=agent, tokens_in=tokens_in, tokens_out=tokens_out,
                cost=cost, latency_ms=latency_ms,
            ))
            db.commit()
    except Exception:
        pass
    try:
        if tokens_in:
            incr("eap_tokens_total", {"direction": "in"}, tokens_in)
        if tokens_out:
            incr("eap_tokens_total", {"direction": "out"}, tokens_out)
    except Exception:
        pass


def _cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    """按模型定价计得成本（单价 = 每百万 token；未配价格 → 0）。"""
    try:
        from sqlalchemy import select

        from ..db import SessionLocal
        from ..models import ModelRecord

        with SessionLocal() as db:
            record = db.scalar(select(ModelRecord).where(ModelRecord.name == model))
            if record is None or (record.price_in is None and record.price_out is None):
                return 0.0
            cost = 0.0
            if record.price_in is not None:
                cost += tokens_in / 1_000_000 * record.price_in
            if record.price_out is not None:
                cost += tokens_out / 1_000_000 * record.price_out
            return round(cost, 6)
    except Exception:
        return 0.0

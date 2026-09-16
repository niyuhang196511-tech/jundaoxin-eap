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
    """全链路 trace_id：入口生成/透传，响应头返回；同步累加进程内 metrics（/metrics 暴露）。"""

    async def dispatch(self, request: Request, call_next):
        from .metrics import incr

        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        request.state.t0 = time.monotonic()
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        try:
            route = request.scope.get("route")
            path_tpl = getattr(route, "path", request.url.path)
            incr("eap_requests_total", {"method": request.method, "path": path_tpl,
                                        "status": str(response.status_code)})
            incr("eap_request_latency_seconds_sum",
                 {"path": path_tpl}, time.monotonic() - request.state.t0)
            logger.info("%s %s -> %s [%s]", request.method, path_tpl,
                        response.status_code, trace_id[:8])
        except Exception:
            pass  # 观测不阻断主流程
        return response


def record_usage(trace_id: str, tenant_id: int, *, kind: str, model: str,
                 tokens_in: int, tokens_out: int, latency_ms: int) -> None:
    """计量事实入库（成本中心数据源）+ token 计数器。失败不影响主流程。"""
    from .metrics import incr

    try:
        with SessionLocal() as db:
            db.add(UsageRecord(
                trace_id=trace_id, tenant_id=tenant_id, kind=kind, model=model,
                tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms,
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

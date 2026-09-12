"""横切·观测：trace_id 中间件 + usage 计量入库（docs/08 §1、§4 的 M1 版）。"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..db import SessionLocal
from ..models import UsageRecord


class TraceMiddleware(BaseHTTPMiddleware):
    """全链路 trace_id：入口生成/透传，响应头返回（OTel 导出在 M2 接入）。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        request.state.t0 = time.monotonic()
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response


def record_usage(trace_id: str, tenant_id: int, *, kind: str, model: str,
                 tokens_in: int, tokens_out: int, latency_ms: int) -> None:
    """计量事实入库（成本中心数据源）。失败不影响主流程。"""
    try:
        with SessionLocal() as db:
            db.add(UsageRecord(
                trace_id=trace_id, tenant_id=tenant_id, kind=kind, model=model,
                tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms,
            ))
            db.commit()
    except Exception:
        pass

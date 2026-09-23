"""请求延迟直方图中间件（M44-C / P4 剩余工作，docs/progress-plan.md 任务组 P4「剩余工作」）。

``eap_request_latency_seconds`` histogram（桶定义见 observability/metrics.HISTOGRAMS），
替代原 TraceMiddleware 里的均值代理计数器 ``eap_request_latency_seconds_sum{path}``
（该计数器已并入本 histogram 的 ``_sum``，避免同名 _sum 双 TYPE 解析冲突）。

注册位置（main.py）：add_middleware LIFO 最后注册 → 最外层，覆盖包括网关 413/429
拒绝在内的用户可感知全链路延迟；不含 Starlette ServerErrorMiddleware 之上的
进程级崩溃响应（那部分在本中间件之外生成）。

labels 取舍（避免基数爆炸，诚实说明）：
- ``method``：GET/POST 等固定小集合；
- ``route``：路由模板（FastAPI 匹配后写入 scope["route"].path，如 ``/api/v1/agents/{name}``），
  而非原始路径——否则路径中的 id 参数会撑爆时间序列；网关拒绝（413/429）与未命中
  路由的请求（404、挂载的静态目录 /media /sdk 等）拿不到 route，统一落 ``unmatched``
  常量标签。刻意不加 status/path 原始值：status 可由 route×流量推断，原始路径高基数。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .metrics import observe

_UNMATCHED = "unmatched"  # 网关拒绝 / 未命中路由（404、静态挂载）时的 route 标签常量


class RequestLatencyMiddleware(BaseHTTPMiddleware):
    """全链路请求延迟打点：call_next 前后计时，finally 中观测（异常路径同样计入）。"""

    async def dispatch(self, request: Request, call_next):
        # perf_counter 而非 monotonic：Windows 上 monotonic 粒度约 15ms，快速请求
        # 前后落在同一刻度会观测到恰好 0 秒（秒级直方图下 _sum 恒 0）；perf_counter
        # 是高分辨率的单调钟（Python 文档推荐用于区间测量）。
        t0 = time.perf_counter()
        try:
            return await call_next(request)
        finally:
            # finally 保证 4xx/5xx（含网关上层拒绝后透传）与异常均打点；
            # 打点自身失败不阻断响应（观测不破坏主流程，与 TraceMiddleware 同约定）
            try:
                route = getattr(request.scope.get("route"), "path", _UNMATCHED)
                observe("eap_request_latency_seconds", time.perf_counter() - t0,
                        {"method": request.method, "route": route or _UNMATCHED})
            except Exception:
                pass

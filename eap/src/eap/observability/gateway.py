"""API Gateway（M31 任务组 F）：并发上限 / 请求体上限 / 超时 / 幂等键 / 熔断器。

三个 BaseHTTPMiddleware + 一个独立熔断器（供 modelhub 路由联动）：

- RequestSizeLimitMiddleware：Content-Length 超过 ``EAP_GATEWAY_MAX_BODY_BYTES`` → 413（0=关闭）
- ConcurrencyLimitMiddleware：按凭证在途请求超过 ``EAP_GATEWAY_MAX_CONCURRENCY`` → 429+Retry-After
  （0=关闭）；内含非流式请求超时（``EAP_GATEWAY_TIMEOUT_S``，0=关闭）→ 504
- IdempotencyMiddleware：``/api/v1/*`` 写方法 + ``Idempotency-Key`` 头 → 同键重放首次响应
  （TTL ``EAP_GATEWAY_IDEMPOTENCY_TTL_S``；进程内存储，配 Redis 后可切 Redis）

请求流经顺序 = RequestSize → Concurrency（含超时）→ Idempotency。
main.py 中按 add_middleware 的 LIFO 语义逆序注册（后注册者在外层）。

SSE 流式路径按前缀排除（``/v1/chat/completions`` 与 ``/a2a``）：超时不包裹
（避免掐断流）、幂等不缓存（响应体无界且语义为增量流）。另配合 Accept 头
含 ``text/event-stream`` 时同样跳过超时。

熔断器 CircuitBreaker：按模型名滑动窗口错误率统计 → open（拒绝）→ 冷却后半开
（单次探测）→ 成功关闭 / 失败再跳闸。asyncio 单事件循环不要求线程安全；
时间源可注入（测试伪时钟）。持久化不做（进程内存态，重启即恢复闭合）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..config import get_settings
from .metrics import gauge_set, incr

# SSE 流式路径排除清单（前缀匹配）：超时与幂等均跳过（docs unfinished.md §三十四）
SSE_EXCLUDE_PREFIXES = ("/v1/chat/completions", "/a2a")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_IDEM_STORE_SWEEP_AT = 1024  # 幂等存储条目数超过该值时触发一次过期清扫


def credential_of(request: Request) -> str:
    """请求凭证标识：API Key（Bearer/X-API-Key，哈希截断防泄漏）→ 无凭证按 client IP。"""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "")
    if token:
        return "key:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    host = request.client.host if request.client else "unknown"
    return "ip:" + host


def is_streaming_request(request: Request) -> bool:
    """流式判断：路径在 SSE 排除清单，或 Accept 头含 text/event-stream。"""
    if request.url.path.startswith(SSE_EXCLUDE_PREFIXES):
        return True
    return "text/event-stream" in request.headers.get("accept", "").lower()


def _reject(status: int, reason: str, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    incr("eap_gateway_rejected_total", {"reason": reason})
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """请求体上限（外层）：Content-Length 超限 → 413（默认 0=关闭）。

    仅校验 Content-Length 头（chunked 无该头的场景不拦——uvicorn 生产前置反代统一收口）。
    """

    async def dispatch(self, request: Request, call_next):
        max_bytes = get_settings().gateway_max_body_bytes
        if max_bytes > 0:
            content_length = request.headers.get("content-length", "")
            if content_length.isdigit() and int(content_length) > max_bytes:
                return _reject(413, "body_size", f"EAP-413 请求体超过上限 {max_bytes} 字节")
        return await call_next(request)


class ConcurrencyLimitMiddleware(BaseHTTPMiddleware):
    """按凭证并发上限 + 非流式请求超时。

    并发：进程内 dict 计数（每凭证在途数），try/finally 保证释放；
    超限 → 429 + Retry-After。超时：内置于本中间件（在并发槽内掐断，
    等待释放槽位），asyncio.wait_for 包裹 call_next → 504；流式请求跳过。
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._inflight: dict[str, int] = {}  # 凭证 → 在途数
        self._total = 0  # 全局在途（eap_gateway_inflight gauge）

    async def dispatch(self, request: Request, call_next):
        limit = get_settings().gateway_max_concurrency
        if limit > 0:
            cred = credential_of(request)
            if self._inflight.get(cred, 0) >= limit:
                return _reject(429, "concurrency", "EAP-429 并发请求超限，请稍后重试",
                               headers={"Retry-After": "1"})
            self._inflight[cred] = self._inflight.get(cred, 0) + 1
        self._total += 1  # 在途 gauge 不受开关影响，始终统计
        gauge_set("eap_gateway_inflight", None, self._total)
        try:
            return await self._with_timeout(request, call_next)
        finally:
            if limit > 0:
                self._inflight[cred] -= 1
                if self._inflight[cred] <= 0:
                    self._inflight.pop(cred, None)
            self._total -= 1
            gauge_set("eap_gateway_inflight", None, self._total)

    async def _with_timeout(self, request: Request, call_next):
        timeout = get_settings().gateway_timeout_s
        if timeout <= 0 or is_streaming_request(request):
            return await call_next(request)
        try:
            return await asyncio.wait_for(call_next(request), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return _reject(504, "timeout", "EAP-504 上游处理超时")


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """写操作幂等键：/api/v1/* 写方法 + Idempotency-Key 头 → 同键重放首次响应。

    key = 凭证标识 + 头值；进程内 ``{key: (expires_at, status, body, content_type)}``
    存储 + 在途 Future 合并（并发同键等首次完成后重放，不重复执行）。
    重放响应带 ``X-Idempotent-Replay: true``。SSE 流式路径按前缀排除。
    配置 EAP_REDIS_URL 后结果写入 Redis（TTL 同配置，多副本共享重放；
    测试仅覆盖进程内分支；跨副本并发不合并——网关级幂等 v1 语义）。
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._store: dict[str, tuple[float, int, bytes, str]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def dispatch(self, request: Request, call_next):
        if request.method not in _WRITE_METHODS:
            return await call_next(request)
        if not request.url.path.startswith("/api/v1/") or is_streaming_request(request):
            return await call_next(request)
        idem_key = request.headers.get("idempotency-key", "")
        if not idem_key:
            return await call_next(request)
        key = f"{credential_of(request)}:{idem_key}"

        remote = await self._redis_get(key)  # Redis 优先（多副本共享重放）
        if remote is not None:
            return self._replay(remote)

        entry = self._store.get(key)
        if entry is not None:
            if entry[0] > time.monotonic():
                return self._replay(entry)
            self._store.pop(key, None)  # TTL 过期清理

        waiter = self._inflight.get(key)
        if waiter is not None:  # 并发同键：等首次执行完成后重放
            try:
                done = await waiter
            except Exception:
                return await self._execute(request, call_next, key)  # 首次失败：自行执行
            return self._replay(done)
        return await self._execute(request, call_next, key)

    async def _execute(self, request: Request, call_next, key: str) -> Response:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            response = await call_next(request)
            body_iter = getattr(response, "body_iterator", None)
            body = (b"".join([chunk async for chunk in body_iter]) if body_iter is not None
                    else response.body)  # 非 BaseHTTPMiddleware 场景（直连 Response）兼容
            entry = (time.monotonic() + get_settings().gateway_idempotency_ttl_s,
                     response.status_code, body, response.headers.get("content-type", ""))
            self._store[key] = entry
            await self._redis_put(key, entry)
            if not fut.done():
                fut.set_result(entry)
            if len(self._store) > _IDEM_STORE_SWEEP_AT:
                self._sweep()
            return self._rebuild(response.status_code, body, response.headers)
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)

    def _replay(self, entry: tuple[float, int, bytes, str]) -> Response:
        incr("eap_idempotent_replays_total")
        _expires_at, status, body, content_type = entry
        resp = Response(content=body, status_code=status)
        if content_type:
            resp.headers["content-type"] = content_type
        resp.headers["X-Idempotent-Replay"] = "true"
        return resp

    @staticmethod
    def _rebuild(status: int, body: bytes, headers) -> Response:
        """重组缓冲后的响应（剔除 content-length 由 Response 重算）。"""
        resp = Response(content=body, status_code=status)
        for name, value in headers.items():
            if name.lower() != "content-length":
                resp.headers.append(name, value)
        return resp

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._store.items() if v[0] <= now]:
            self._store.pop(k, None)

    # ---------- Redis 可选分支（EAP_REDIS_URL 配置即启用；抖动回退进程内） ----------

    async def _redis_get(self, key: str) -> tuple[float, int, bytes, str] | None:
        url = get_settings().redis_url
        if not url:
            return None
        try:
            import base64
            import json as _json

            import redis.asyncio as aioredis

            r = aioredis.from_url(url, decode_responses=True)
            try:
                raw = await r.get(f"eap:idem:{key}")
            finally:
                await r.aclose()
            if raw is None:
                return None
            data = _json.loads(raw)
            # 本地 expires_at 填 0.0：Redis TTL 已治理过期，重放前不再本地校验
            return (0.0, int(data["status"]), base64.b64decode(data["body"]), data["content_type"])
        except Exception:
            return None

    async def _redis_put(self, key: str, entry: tuple[float, int, bytes, str]) -> None:
        url = get_settings().redis_url
        if not url:
            return
        try:
            import base64
            import json as _json

            import redis.asyncio as aioredis

            _expires_at, status, body, content_type = entry
            ttl = max(1, int(get_settings().gateway_idempotency_ttl_s))
            r = aioredis.from_url(url, decode_responses=True)
            try:
                await r.set(f"eap:idem:{key}", _json.dumps({
                    "status": status, "body": base64.b64encode(body).decode(),
                    "content_type": content_type}), ex=ttl)
            finally:
                await r.aclose()
        except Exception:
            pass  # Redis 抖动不阻断主流程（进程内存储已生效）


class CircuitBreaker:
    """按模型滑动窗口错误率熔断：closed → open →（冷却）half-open → 成功 close / 失败 re-open。

    - 滑动窗口：每模型 deque[(时间, 成功?)]，超窗口样本剪除；
    - 跳闸条件：窗口内样本数 ≥ min_samples 且失败率 ≥ rate → open；
    - half-open：冷却（cooldown_s）后放行单次探测（其余请求仍拒绝），成功→closed、失败→re-open；
    - 时间源 clock 可注入（默认 time.monotonic；测试用伪时钟）。
    """

    def __init__(self, *, window_s: float | None = None, rate: float | None = None,
                 min_samples: int | None = None, cooldown_s: float | None = None,
                 clock=None) -> None:
        s = get_settings()
        self.window_s = s.gateway_cb_window_s if window_s is None else window_s
        self.rate = s.gateway_cb_rate if rate is None else rate
        self.min_samples = s.gateway_cb_min_samples if min_samples is None else min_samples
        self.cooldown_s = s.gateway_cb_cooldown_s if cooldown_s is None else cooldown_s
        self._clock = clock or time.monotonic
        self._samples: dict[str, deque[tuple[float, bool]]] = defaultdict(deque)
        self._opened_at: dict[str, float] = {}
        self._probing: set[str] = set()

    # ---------- 状态 ----------

    def state(self, model: str) -> int:
        """0=closed / 1=open / 2=half-open（open 且已过冷却）。"""
        if model not in self._opened_at:
            return 0
        return 2 if self._clock() - self._opened_at[model] >= self.cooldown_s else 1

    def allow(self, model: str) -> bool:
        """路由过滤点：open 拒绝（模型从降级链剔除）；half-open 放单次探测。"""
        st = self.state(model)
        if st == 1:
            gauge_set("eap_circuit_state", {"model": model}, 1)
            return False
        if st == 2:
            if model in self._probing:  # 探测期间单飞
                return False
            self._probing.add(model)
            gauge_set("eap_circuit_state", {"model": model}, 2)
            return True
        gauge_set("eap_circuit_state", {"model": model}, 0)
        return True

    # ---------- 记录 ----------

    def record_success(self, model: str) -> None:
        self._probing.discard(model)
        if model in self._opened_at:  # half-open 探测成功 → close
            self._opened_at.pop(model, None)
            self._samples.pop(model, None)
        else:
            self._prune(model)
            self._samples[model].append((self._clock(), True))
            self._maybe_trip(model)  # 成功事件也重估：样本数刚满且历史失败率达阈值时跳闸
        gauge_set("eap_circuit_state", {"model": model}, self.state(model))

    def record_failure(self, model: str) -> None:
        self._probing.discard(model)
        if model in self._opened_at:  # half-open 探测失败 → re-open（冷却重新计时）
            self._opened_at[model] = self._clock()
        else:
            self._prune(model)
            self._samples[model].append((self._clock(), False))
            self._maybe_trip(model)
        gauge_set("eap_circuit_state", {"model": model}, self.state(model))

    # ---------- 内部 ----------

    def _maybe_trip(self, model: str) -> None:
        """窗口样本数 ≥ min_samples 且失败率 ≥ rate → 跳闸。"""
        dq = self._samples[model]
        if len(dq) < self.min_samples:
            return
        failures = sum(1 for _, ok in dq if not ok)
        if failures / len(dq) >= self.rate:
            self._trip(model)

    def _trip(self, model: str) -> None:
        self._opened_at[model] = self._clock()
        self._samples.pop(model, None)
        incr("eap_circuit_trips_total", {"model": model})

    def _prune(self, model: str) -> None:
        now = self._clock()
        dq = self._samples[model]
        while dq and now - dq[0][0] > self.window_s:
            dq.popleft()


circuit_breaker = CircuitBreaker()  # 平台单例（modelhub 路由联动）


def get_breaker() -> CircuitBreaker:
    """路由器取熔断器（测试可 monkeypatch eap.observability.gateway.circuit_breaker）。"""
    return circuit_breaker

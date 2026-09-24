"""API Gateway（M31 任务组 F / M49-B）：并发上限 / 请求体上限 / 超时 / 幂等键 / 熔断器。

三个 BaseHTTPMiddleware + 一个独立熔断器（供 modelhub 路由联动）：

- RequestSizeLimitMiddleware：Content-Length 超过 ``EAP_GATEWAY_MAX_BODY_BYTES`` → 413（0=关闭）
- ConcurrencyLimitMiddleware：按凭证在途请求超过 ``EAP_GATEWAY_MAX_CONCURRENCY`` → 429+Retry-After
  （0=关闭）；内含非流式请求超时（``EAP_GATEWAY_TIMEOUT_S``，0=关闭）→ 504
- IdempotencyMiddleware：``/api/v1/*`` 写方法 + ``Idempotency-Key`` 头 → 同键重放首次响应
  （TTL ``EAP_GATEWAY_IDEMPOTENCY_TTL_S``）

请求流经顺序 = RequestSize → Concurrency（含超时）→ Idempotency。
main.py 中按 add_middleware 的 LIFO 语义逆序注册（后注册者在外层）。

SSE 流式路径按前缀排除（``/v1/chat/completions`` 与 ``/a2a``）：超时不包裹
（避免掐断流）、幂等不缓存（响应体无界且语义为增量流）。另配合 Accept 头
含 ``text/event-stream`` 时同样跳过超时。

熔断器 CircuitBreaker：按模型名滑动窗口错误率统计 → open（拒绝）→ 冷却后半开
（单次探测）→ 成功关闭 / 失败再跳闸。asyncio 单事件循环不要求线程安全；
时间源可注入（测试伪时钟）。

分布式态（M49-B，对齐 api/security.py SlidingWindow 范式）：配置 ``EAP_REDIS_URL``
后三个机制跨副本共享——并发计数 INCR/DECR（eap:gw:conc:*）、幂等在途 SETNX 标记
（eap:idem:inflight:*）、熔断状态/滑窗样本落键（eap:gw:cb:*）；Redis 未配置或
不可达一律回退进程内态（行为与单副本现状一致），降级告警每机制只发一次不刷日志。
熔断器对外 API 保持同步（router.py 在事件循环内同步调用，不改其调用面），
Redis 分支用同步客户端 + 0.2s 短超时，最坏阻塞即超时值——取舍见 CircuitBreaker 注释。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict, deque
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..config import get_settings
from .metrics import gauge_set, incr

logger = logging.getLogger("eap.observability.gateway")

# SSE 流式路径排除清单（前缀匹配）：超时与幂等均跳过（docs unfinished.md §三十四）
SSE_EXCLUDE_PREFIXES = ("/v1/chat/completions", "/a2a")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_IDEM_STORE_SWEEP_AT = 1024  # 幂等存储条目数超过该值时触发一次过期清扫

# ---------- Redis 分布式态公共参数（M49-B） ----------
_REDIS_SOCK_TIMEOUT_S = 0.5  # 异步客户端 socket/连接超时：不可达/黑洞时快速失败走回退
_CB_REDIS_TIMEOUT_S = 0.2  # 熔断同步客户端超时（事件循环内调用，最坏阻塞压到最小）
_CONC_TTL_FLOOR_S = 120  # 在途计数键 TTL 下限（timeout=0 关闭时的兜底，见 _concurrency_ttl_s）
_IDEM_INFLIGHT_TTL_FLOOR_S = 60  # 幂等在途标记 TTL 下限（防执行方崩溃死锁）
_IDEM_INFLIGHT_WAIT_S = 2.0  # 跨副本后到者轮询等待上限（测试可 monkeypatch 缩短）
_IDEM_INFLIGHT_POLL_S = 0.05  # 轮询间隔

_DEGRADE_WARNED: set[str] = set()  # 降级告警去重（每机制每进程一次）


def _warn_degraded(mechanism: str, exc: Exception) -> None:
    """Redis 降级告警只发一次（按机制），不刷日志：后续失败静默回退进程内路径。"""
    if mechanism in _DEGRADE_WARNED:
        return
    _DEGRADE_WARNED.add(mechanism)
    logger.warning("网关 %s：Redis 不可用（%s），回退进程内态（多副本共享失效）；本告警仅提示一次",
                   mechanism, exc)


def _aioredis_client(url: str):
    """异步 Redis 客户端统一工厂：短超时 + decode_responses（不可达时快速抛错走回退）。"""
    import redis.asyncio as aioredis

    return aioredis.from_url(url, decode_responses=True, socket_timeout=_REDIS_SOCK_TIMEOUT_S,
                             socket_connect_timeout=_REDIS_SOCK_TIMEOUT_S)


def _concurrency_ttl_s() -> int:
    """并发在途计数键 TTL：请求超时上限的 4 倍，下限 120s。

    取舍：非流式请求要么被超时（EAP_GATEWAY_TIMEOUT_S）掐断、要么远早于其完成，
    4× 给慢上游与副本间时钟偏差留余量；timeout=0（关闭）时按下限 120s——崩溃副本
    泄漏的在途计数最多 2 分钟自愈。代价：合法在途超过 TTL 且无新流量续期的请求
    会丢失计数（方向 fail-open：短暂多放行，不会误拒）。
    """
    timeout = get_settings().gateway_timeout_s
    return int(max(timeout * 4, _CONC_TTL_FLOOR_S))


def _idem_inflight_ttl_s() -> int:
    """幂等在途标记 TTL：请求超时上限的 2 倍，下限 60s（执行方崩溃防死锁）。

    取舍：timeout=0（关闭）时合法执行超 60s 会丢标记，后到者可能接管造成重复执行
    （完成后覆盖已存响应，last-write-wins）——崩溃死锁概率高于超长执行，取小 TTL。
    """
    timeout = get_settings().gateway_timeout_s
    return max(_IDEM_INFLIGHT_TTL_FLOOR_S, int(timeout * 2))


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

    并发：配 EAP_REDIS_URL 时跨副本共享计数（键 eap:gw:conc:<凭证>，入场
    INCR+EXPIRE 走 MULTI 原子，INCR 后计数超限即 DECR 回滚再按现状语义 429；
    限额判断基于 Redis 计数值，全局限额 = limit 而非 limit × 副本数）；
    未配置/不可达回退进程内 dict 计数（现状：各副本独立限额）。
    try/finally 保证释放；键 TTL 防进程崩溃泄漏（取舍见 _concurrency_ttl_s）。
    超时：内置于本中间件（在并发槽内掐断，等待释放槽位），
    asyncio.wait_for 包裹 call_next → 504；流式请求跳过。
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._inflight: dict[str, int] = {}  # 凭证 → 在途数（进程内回退路径）
        self._total = 0  # 全局在途（eap_gateway_inflight gauge，本副本口径）

    async def dispatch(self, request: Request, call_next):
        limit = get_settings().gateway_max_concurrency
        shared = False  # 本请求槽位是否记账在 Redis（决定 finally 的释放路径）
        if limit > 0:
            cred = credential_of(request)
            acquired = await self._redis_acquire(cred, limit)
            if acquired is False:
                return _reject(429, "concurrency", "EAP-429 并发请求超限，请稍后重试",
                               headers={"Retry-After": "1"})
            if acquired is True:
                shared = True
            elif self._inflight.get(cred, 0) >= limit:  # None=Redis 未配置/不可达 → 进程内（现状）
                return _reject(429, "concurrency", "EAP-429 并发请求超限，请稍后重试",
                               headers={"Retry-After": "1"})
            else:
                self._inflight[cred] = self._inflight.get(cred, 0) + 1
        self._total += 1  # 在途 gauge 不受开关影响，始终统计
        gauge_set("eap_gateway_inflight", None, self._total)
        try:
            return await self._with_timeout(request, call_next)
        finally:
            if limit > 0:
                if shared:
                    await self._redis_release(cred)  # 取消传播中此步失败时由 TTL 兜底回收
                else:
                    self._inflight[cred] -= 1
                    if self._inflight[cred] <= 0:
                        self._inflight.pop(cred, None)
            self._total -= 1
            gauge_set("eap_gateway_inflight", None, self._total)

    async def _redis_acquire(self, cred: str, limit: int) -> bool | None:
        """Redis 跨副本占槽：INCR+EXPIRE（MULTI 原子）；超限 DECR 回滚后拒绝。

        返回 None = Redis 未配置/不可达（调用方回退进程内计数，行为与现状一致）。
        INCR 与回滚 DECR 之间进程崩溃会残留 +1 计数，由键 TTL 自愈（fail-open 方向）。
        """
        url = get_settings().redis_url
        if not url:
            return None
        key = f"eap:gw:conc:{cred}"
        try:
            r = _aioredis_client(url)
            try:
                pipe = r.pipeline(transaction=True)
                pipe.incr(key)
                pipe.expire(key, _concurrency_ttl_s())  # 每次进出场续期：有流量期间键不失效
                count = int((await pipe.execute())[0])
                if count > limit:
                    await r.decr(key)  # 超限回滚：被拒请求不占槽
                    return False
                return True
            finally:
                await r.aclose()
        except Exception as e:
            _warn_degraded("concurrency", e)
            return None

    async def _redis_release(self, cred: str) -> None:
        """释放槽位：DECR+EXPIRE；负值 = TTL 中途过期造成的计数漂移，重置归零基线。"""
        url = get_settings().redis_url
        if not url:
            return
        key = f"eap:gw:conc:{cred}"
        try:
            r = _aioredis_client(url)
            try:
                pipe = r.pipeline(transaction=True)
                pipe.decr(key)
                pipe.expire(key, _concurrency_ttl_s())
                val = int((await pipe.execute())[0])
                if val < 0:
                    await r.set(key, 0, ex=_concurrency_ttl_s())
            finally:
                await r.aclose()
        except Exception as e:
            _warn_degraded("concurrency", e)  # 残留计数等 TTL 自愈（fail-open：可能漏拒不误拒）

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
    配置 EAP_REDIS_URL 后结果写入 Redis（TTL 同配置，多副本共享重放）。
    跨副本在途（M49-B）：SETNX 在途标记 ``eap:idem:inflight:<key>``（TTL 防执行方
    崩溃死锁，见 _idem_inflight_ttl_s）——抢到标记者执行；后到副本短暂轮询
    （_IDEM_INFLIGHT_WAIT_S，默认 2s）等已存响应重放，超时返回 409+Retry-After
    （取舍：宁可短暂拒绝，不容忍第二副本重复执行副作用）；首次执行失败会清除
    标记，后到者检测消失后 SETNX 竞争接管执行（对齐进程内 Future 合并的
    「首次失败 → 自行执行」语义）。本进程 Future 合并保留。Redis 不可达
    回退进程内语义（跨副本不合并）。测试见 tests/test_gateway_redis.py。
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
                return await self._execute_shared(request, call_next, key)  # 首次失败：自行执行
            return self._replay(done)
        return await self._execute_shared(request, call_next, key)

    # ---------- 跨副本在途协同（M49-B：SETNX 在途标记 + 短轮询重放） ----------

    async def _execute_shared(self, request: Request, call_next, key: str) -> Response:
        """执行入口：Redis 配置时先抢跨副本在途标记；未配置/不可达退化为进程内语义。"""
        marked = await self._redis_mark_inflight(key)
        if marked is None:  # Redis 未配置/不可达 → 现状进程内路径
            return await self._execute(request, call_next, key)
        if marked:
            try:
                return await self._execute(request, call_next, key)
            finally:
                await self._redis_clear_inflight(key)  # 失败也清标记：让后到者可接管
        # 后到者（另一副本执行中）：轮询已存响应 → 重放；标记消失（首次失败）→ 接管；超时 → 409
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _IDEM_INFLIGHT_WAIT_S
        while loop.time() < deadline:
            remote = await self._redis_get(key)
            if remote is not None:
                return self._replay(remote)
            if not await self._redis_inflight_exists(key):
                break  # 首次执行已退出且未存结果（失败/崩溃清理）→ 尝试接管
            await asyncio.sleep(_IDEM_INFLIGHT_POLL_S)
        else:
            return self._idem_conflict()
        marked = await self._redis_mark_inflight(key)
        if marked is False:  # 其他等待副本抢先接管
            return self._idem_conflict()
        try:  # None（Redis 退化）或 True（接管成功）→ 执行
            return await self._execute(request, call_next, key)
        finally:
            if marked:
                await self._redis_clear_inflight(key)

    @staticmethod
    def _idem_conflict() -> JSONResponse:
        return _reject(409, "idempotency_inflight",
                       "EAP-409 同幂等键请求正在处理中，请稍后重试",
                       headers={"Retry-After": "1"})

    async def _redis_mark_inflight(self, key: str) -> bool | None:
        """SETNX 在途标记（TTL 防执行方崩溃死锁）。None = Redis 未配置/不可达。"""
        url = get_settings().redis_url
        if not url:
            return None
        try:
            r = _aioredis_client(url)
            try:
                got = await r.set(f"eap:idem:inflight:{key}", "1", nx=True,
                                  ex=_idem_inflight_ttl_s())
                return bool(got)
            finally:
                await r.aclose()
        except Exception as e:
            _warn_degraded("idempotency", e)
            return None

    async def _redis_inflight_exists(self, key: str) -> bool:
        url = get_settings().redis_url
        if not url:
            return False
        try:
            r = _aioredis_client(url)
            try:
                return bool(await r.exists(f"eap:idem:inflight:{key}"))
            finally:
                await r.aclose()
        except Exception:
            return False  # 探测失败视同标记不存在 → 走接管分支（其降级由 mark 自行处理）

    async def _redis_clear_inflight(self, key: str) -> None:
        url = get_settings().redis_url
        if not url:
            return
        try:
            r = _aioredis_client(url)
            try:
                await r.delete(f"eap:idem:inflight:{key}")
            finally:
                await r.aclose()
        except Exception as e:
            _warn_degraded("idempotency", e)  # 残留标记等 TTL 过期（期间后到者 409）

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

            r = _aioredis_client(url)
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

            _expires_at, status, body, content_type = entry
            ttl = max(1, int(get_settings().gateway_idempotency_ttl_s))
            r = _aioredis_client(url)
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

    Redis 状态共享（M49-B，配 EAP_REDIS_URL 启用；未配置/不可达回退进程内，行为同现状）：
    - 状态键 ``eap:gw:cb:<model>:open``（值 = 跳闸时刻，取自可注入 clock，跨副本须同源；
      TTL = 10×cooldown 且 ≥300s——键过期视同 closed，等价一次自动恢复，防永久泄漏。
      取代旧「持久化不做（重启即恢复闭合）」语义：状态出进程，重启不再丢熔断记忆）；
    - 滑窗样本：``:total`` / ``:fail`` 双 ZSET（score = clock 值，成员唯一）；每次记录在
      单个 MULTI pipeline 内完成剪除（ZREMRANGEBYSCORE）+ 写入 + 计数（ZCARD×2），
      按计数评估跳闸后再第二次写入 open 键——两副本同时达阈值会先后 SET open
      （opened_at 毫秒级差异，无害，容忍）；
    - half-open 探测单飞：``:probe`` 键 SET NX EX（TTL = max(cooldown, 30s)，探测方
      崩溃自动释放）。常态下同一时刻全局仅一个副本探测；probe TTL 过期而原探测
      仍在途时，另一副本可能发起第二次探测——多副本同时试探是可接受的放松语义
      （探测即一次真实上游调用，失败只会 re-open，不破坏安全性）；
    - 对外 API 保持同步（router.py 在事件循环内同步调用，不改其调用面）：Redis 分支
      用同步客户端 + 0.2s 短超时，最坏单次阻塞即超时值；每次调用一个往返
      （记录路径 2 个），局域网 Redis 亚毫秒级——以微小阻塞换取零调用面改动。
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
        self._rcli = None  # 同步 Redis 客户端（惰性创建；URL 变化重建。同步客户端无事件循环绑定问题）
        self._rurl: str | None = None

    # ---------- Redis 分支（M49-B） ----------

    def _rclient(self):
        """惰性取同步 Redis 客户端；未配置/创建失败返回 None（调用方走进程内路径）。"""
        url = get_settings().redis_url
        if not url:
            return None
        if self._rcli is not None and self._rurl == url:
            return self._rcli
        try:
            import redis as redis_sync

            self._rcli = redis_sync.Redis.from_url(
                url, decode_responses=True, socket_timeout=_CB_REDIS_TIMEOUT_S,
                socket_connect_timeout=_CB_REDIS_TIMEOUT_S)
            self._rurl = url
            return self._rcli
        except Exception as e:
            _warn_degraded("circuit", e)
            return None

    @staticmethod
    def _keys(model: str) -> tuple[str, str, str, str]:
        base = f"eap:gw:cb:{model}"
        return base + ":open", base + ":total", base + ":fail", base + ":probe"

    def _open_ttl(self) -> int:
        return max(int(self.cooldown_s) * 10, 300)

    def _r_record(self, model: str, *, ok: bool, r) -> None:
        """Redis 状态迁移：open/half-open 期处理探测结果；否则滑窗记样本 + 评估跳闸。"""
        open_key, total_key, fail_key, probe_key = self._keys(model)
        now = self._clock()
        if r.exists(open_key):
            pipe = r.pipeline(transaction=True)
            if ok:  # half-open 探测成功 → close（清空全部共享状态）
                pipe.delete(open_key, total_key, fail_key, probe_key)
            else:  # 探测失败 → re-open（冷却重新计时）
                pipe.set(open_key, repr(now), ex=self._open_ttl())
                pipe.delete(probe_key)
            pipe.execute()
            return
        cutoff = now - self.window_s
        member = f"{now:.6f}:{uuid4().hex[:8]}"  # 唯一成员（同成员 ZADD 会覆盖计数）
        ttl = max(int(self.window_s * 2), 60)  # 样本键 TTL：两倍窗口，防泄漏
        pipe = r.pipeline(transaction=True)
        pipe.zremrangebyscore(total_key, "-inf", cutoff)
        pipe.zremrangebyscore(fail_key, "-inf", cutoff)
        pipe.zadd(total_key, {member: now})
        if not ok:
            pipe.zadd(fail_key, {member: now})
        pipe.expire(total_key, ttl)
        pipe.expire(fail_key, ttl)
        pipe.zcard(total_key)
        pipe.zcard(fail_key)
        total, fails = pipe.execute()[-2:]
        if total >= self.min_samples and fails / total >= self.rate:
            pipe = r.pipeline(transaction=True)  # 跳闸与评估非同一事务：并发双跳闸仅 opened_at 毫秒级差异
            pipe.set(open_key, repr(now), ex=self._open_ttl())
            pipe.delete(total_key, fail_key, probe_key)
            pipe.execute()
            incr("eap_circuit_trips_total", {"model": model})

    # ---------- 状态 ----------

    def state(self, model: str) -> int:
        """0=closed / 1=open / 2=half-open（open 且已过冷却）。Redis 配置时读共享状态键。"""
        r = self._rclient()
        if r is not None:
            try:
                raw = r.get(self._keys(model)[0])
                if raw is None:
                    return 0
                return 2 if self._clock() - float(raw) >= self.cooldown_s else 1
            except Exception as e:
                _warn_degraded("circuit", e)  # 抖动回退进程内态（outage 期间以本地状态为准）
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
            if not self._acquire_probe(model):  # 探测期间单飞（Redis SETNX 跨副本 / 进程内 set）
                return False
            gauge_set("eap_circuit_state", {"model": model}, 2)
            return True
        gauge_set("eap_circuit_state", {"model": model}, 0)
        return True

    def _acquire_probe(self, model: str) -> bool:
        r = self._rclient()
        if r is not None:
            try:
                return bool(r.set(self._keys(model)[3], "1", nx=True,
                                  ex=max(int(self.cooldown_s), 30)))
            except Exception as e:
                _warn_degraded("circuit", e)
        if model in self._probing:
            return False
        self._probing.add(model)
        return True

    # ---------- 记录 ----------

    def record_success(self, model: str) -> None:
        r = self._rclient()
        if r is not None:
            try:
                self._r_record(model, ok=True, r=r)
                gauge_set("eap_circuit_state", {"model": model}, self.state(model))
                return
            except Exception as e:
                _warn_degraded("circuit", e)  # 降级期间本地记样本（与 Redis 态互不合并，恢复后以 Redis 为准）
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
        r = self._rclient()
        if r is not None:
            try:
                self._r_record(model, ok=False, r=r)
                gauge_set("eap_circuit_state", {"model": model}, self.state(model))
                return
            except Exception as e:
                _warn_degraded("circuit", e)
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

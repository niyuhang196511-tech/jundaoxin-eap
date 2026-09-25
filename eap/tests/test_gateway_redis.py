"""网关分布式态 Redis 集成测试（M49-B）：并发计数 / 熔断状态 / 幂等在途跨副本共享。

两组用例：
- 回退路径（无 Redis 配置）：三个机制行为与现状（单副本进程内）一致，离线可跑；
- Redis 路径（skipif 不可达）：同一进程内两个中间件/熔断器实例共享同一 Redis
  （= 两副本），断言跨实例的拒绝/重放/状态可见性。

需要本机 Redis：默认 localhost:63790 db6（与 test_tasks_redis 的 db5 隔离防撞键），
可用 EAP_TEST_GATEWAY_REDIS_URL 覆盖。
键清理纪律：redis 用例前后 SCAN+DELETE db6 内 eap:gw:* / eap:idem:* 键；
用例自身全部使用唯一凭证/模型名/幂等键，双保险不污染共享实例。

M54-A 时序加固（docs/progress-plan.md M53 遗留登记①）：并发计数用例在 PG 全量
脏库复跑高负载下偶发红。机理 = src 的 Redis 操作预算（异步 0.5s / 熔断同步 0.2s）
在负载下被打穿 → 静默回退进程内计数（共享语义前提丢失）。加固三板斧，均不削弱断言：
1) redis_url 夹具内放大操作预算（环境抖动隔离，非语义放宽）；
2) 关键批次前以 _poll_shared_value 做「共享态收敛等待」——deadline 内轮询 Redis
   键值达预期才继续，超时才红；
3) 该轮询同时是反向自证探针：若实现旁路 Redis 退化进程内计数（真 bug），键值
   永远到不了预期 → 显式红且信息明确——用例仍能真正抓错，而非放宽成恒真。
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

REDIS_URL = os.environ.get("EAP_TEST_GATEWAY_REDIS_URL", "redis://localhost:63790/6")


def _redis_alive() -> bool:
    u = urlparse(REDIS_URL)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 6379), timeout=1):
            return True
    except OSError:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_alive(), reason="本机 Redis 不可达（docker compose up -d redis 启动）")


# ---------- 公共工具/夹具 ----------

def _cleanup_keys() -> None:
    """db6 内网关键清理（eap:gw:* / eap:idem:*），用例前后各跑一次。"""
    import redis as redis_sync

    r = redis_sync.from_url(REDIS_URL, decode_responses=True)
    try:
        for pattern in ("eap:gw:*", "eap:idem:*"):
            for k in r.scan_iter(match=pattern, count=500):
                r.delete(k)
    finally:
        r.close()


# ---------- M54-A 时序加固参数（见模块 docstring） ----------
_TEST_REDIS_OP_TIMEOUT_S = 3.0  # 异步客户端操作预算：0.5s → 3s（隔离负载抖动）
_TEST_CB_REDIS_TIMEOUT_S = 1.0  # 熔断同步客户端预算：0.2s → 1s（同上）
_POLL_DEADLINE_S = 5.0  # 共享态收敛等待预算
_POLL_INTERVAL_S = 0.02


def _poll_shared_value(key: str, acceptable, *, url: str = REDIS_URL,
                       timeout_s: float = _POLL_DEADLINE_S) -> str | None:
    """deadline 内轮询共享键值直至 acceptable(val) 成立；超时返回最后观测值（调用方断言）。

    M54-A 双重身份：收敛等待 + 共享路径反向自证探针——实现若旁路 Redis 退化
    进程内计数（真 bug / 静默降级），键值到不了预期 → 超时后断言红且信息明确。
    同步客户端轮询：仅用于被测任务已停泊（事件/槽位已定）的确定性时点，阻塞无碍。
    """
    import time as _time

    import redis as redis_sync

    r = redis_sync.from_url(url, decode_responses=True, socket_timeout=2.0,
                            socket_connect_timeout=2.0)
    try:
        deadline = _time.monotonic() + timeout_s
        val = None
        while True:
            val = r.get(key)
            if acceptable(val):
                return val
            if _time.monotonic() >= deadline:
                return val
            _time.sleep(_POLL_INTERVAL_S)
    finally:
        r.close()


async def _poll_inflight_marked(url: str, key_suffix: str,
                                *, timeout_s: float = _POLL_DEADLINE_S) -> bool:
    """deadline 内轮询等幂等在途标记（SETNX）可见于 Redis；超时返回 False。

    M54-A：替代「固定 sleep 后假定先到者已抢到标记」的概率性时序——负载下
    SETNX 完成时刻不定，后到者若先抢到标记会让 409/合并类断言失去前提。
    标记始终不可见 = 共享路径未生效，调用方显式红（反向自证）。
    """
    from eap.observability import gateway

    r = gateway._aioredis_client(url)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if await r.keys(f"eap:idem:inflight:*{key_suffix}"):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        await r.aclose()


def _scope(method: str = "POST", path: str = "/x", headers: list[tuple[str, str]] | None = None) -> dict:
    """最小 ASGI scope（中间件直接单测用，与 test_gateway.py 同款）。"""
    return {"type": "http", "asgi": {"version": "3.0"}, "method": method, "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
            "query_string": b"", "client": ("127.0.0.1", 1234)}


class _FakeClock:
    """伪时钟（注入 CircuitBreaker，确定性推进；两实例共享同一时钟 = 跨副本同源时间）。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _reset_degrade_warnings():
    """降级告警「每机制一次」的去重集合按用例复位（否则跨用例吞告警）。"""
    from eap.observability import gateway

    gateway._DEGRADE_WARNED.clear()
    yield
    gateway._DEGRADE_WARNED.clear()


@pytest.fixture()
def gw_settings(monkeypatch):
    """网关配置：防护默认关、Redis 显式未配置（回退路径基线；请求时读取，patch 即生效）。"""
    from eap.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "gateway_max_concurrency", 0)
    monkeypatch.setattr(s, "gateway_max_body_bytes", 0)
    monkeypatch.setattr(s, "gateway_timeout_s", 0.0)
    monkeypatch.setattr(s, "gateway_idempotency_ttl_s", 300)
    monkeypatch.setattr(s, "redis_url", None)
    return s


@pytest.fixture()
def redis_url(gw_settings, monkeypatch):
    """Redis 路径：配置指向测试 db6，用例前后清键。

    M54-A：放大 src 的 Redis 操作预算（异步 0.5s→3s、熔断同步 0.2s→1s）——
    预算是生产调优常数而非被测语义，负载下被打穿会静默回退进程内态（共享前提
    丢失 → 偶发红）。共享路径是否真的走 Redis 不靠预算兜底，由用例内
    _poll_shared_value 探针显式守门（退化即显式红）。
    """
    from eap.observability import gateway

    if not _redis_alive():
        pytest.skip("本机 Redis 不可达")
    _cleanup_keys()
    monkeypatch.setattr(gw_settings, "redis_url", REDIS_URL)
    monkeypatch.setattr(gateway, "_REDIS_SOCK_TIMEOUT_S", _TEST_REDIS_OP_TIMEOUT_S)
    monkeypatch.setattr(gateway, "_CB_REDIS_TIMEOUT_S", _TEST_CB_REDIS_TIMEOUT_S)
    yield REDIS_URL
    _cleanup_keys()


def _breaker(clock: _FakeClock, **kw):
    from eap.observability.gateway import CircuitBreaker

    defaults = dict(window_s=60, rate=0.5, min_samples=4, cooldown_s=30, clock=clock)
    defaults.update(kw)
    return CircuitBreaker(**defaults)


# ---------- 回退路径（无 Redis 配置）：三个机制行为与现状一致 ----------

def test_fallback_concurrency_rejects_and_releases(gw_settings):
    """进程内并发限制现状语义：同实例超限 429+Retry-After、finally 释放；跨实例不共享（各自限额）。"""
    from eap.observability.gateway import ConcurrencyLimitMiddleware

    gw_settings.gateway_max_concurrency = 1
    mw_a = ConcurrencyLimitMiddleware(app=None)
    mw_b = ConcurrencyLimitMiddleware(app=None)
    auth = [("authorization", "Bearer " + uuid.uuid4().hex)]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    async def slow(request: Request) -> JSONResponse:
        calls["n"] += 1
        entered.set()
        await release.wait()
        return JSONResponse({"ok": True})

    async def fast(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"ok": True})

    async def scenario():
        t = asyncio.create_task(mw_a.dispatch(Request(_scope(headers=auth)), slow))
        await entered.wait()
        r_same = await mw_a.dispatch(Request(_scope(headers=auth)), fast)
        assert r_same.status_code == 429 and r_same.headers["retry-after"] == "1"
        r_other = await mw_b.dispatch(Request(_scope(headers=auth)), fast)
        assert r_other.status_code == 200  # 无 Redis：实例独立计数（= 现状多副本语义）
        release.set()
        assert (await t).status_code == 200
        r_after = await mw_a.dispatch(Request(_scope(headers=auth)), fast)
        assert r_after.status_code == 200  # 槽位已释放

    asyncio.run(scenario())
    assert calls["n"] == 3  # 被拒请求未触达下游


def test_fallback_breaker_lifecycle(gw_settings):
    """进程内熔断现状语义：连败跳闸 → 冷却半开（探测单飞）→ 成功关闭。"""
    clock = _FakeClock()
    br = _breaker(clock)
    model = "fb-" + uuid.uuid4().hex[:8]
    assert br.state(model) == 0 and br.allow(model)
    for _ in range(4):
        br.record_failure(model)
    assert br.state(model) == 1 and not br.allow(model)
    clock.advance(31)
    assert br.state(model) == 2
    assert br.allow(model) and not br.allow(model)  # 探测期间单飞
    br.record_success(model)
    assert br.state(model) == 0 and br.allow(model)


def test_fallback_idempotency_replay_and_merge(gw_settings):
    """进程内幂等现状语义：同键重放（带标记）、并发同键 Future 合并只执行一次。"""
    from eap.observability.gateway import IdempotencyMiddleware

    mw = IdempotencyMiddleware(app=None)
    calls = {"n": 0}
    k1, k2 = uuid.uuid4().hex, uuid.uuid4().hex

    async def call_next(request: Request) -> JSONResponse:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return JSONResponse({"exec": calls["n"]})

    async def send(key: str):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        r1 = await send(k1)
        assert r1.status_code == 200 and "x-idempotent-replay" not in r1.headers
        r2 = await send(k1)
        assert r2.headers["x-idempotent-replay"] == "true" and r2.body == r1.body
        ra, rb = await asyncio.gather(send(k2), send(k2))  # 并发同键合并
        assert ra.body == rb.body
        assert sorted("x-idempotent-replay" in h.headers for h in (ra, rb)) == [False, True]
        assert calls["n"] == 2  # k1 一次 + k2 合并一次

    asyncio.run(scenario())


class _LogCatcher:
    """替身 logger：直接截获 gateway 模块的 warning 调用。

    不用 caplog——全量套件中先跑的模块可能重配根 logging（basicConfig 等），
    根 handler 捕获不稳定；模块级 logger 替身与全局日志配置完全解耦。
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, msg, *args) -> None:
        self.messages.append(msg % args if args else msg)


def test_unreachable_redis_degrades_to_inprocess_and_warns_once(gw_settings, monkeypatch):
    """EAP_REDIS_URL 指向死端口：三个机制回退进程内且行为与现状一致；降级告警每机制仅一条。"""
    from eap.observability import gateway

    gw_settings.redis_url = "redis://127.0.0.1:63999/0"  # 无监听 → 立即拒连
    catcher = _LogCatcher()
    monkeypatch.setattr(gateway, "logger", catcher)
    CircuitBreaker = gateway.CircuitBreaker
    ConcurrencyLimitMiddleware = gateway.ConcurrencyLimitMiddleware
    IdempotencyMiddleware = gateway.IdempotencyMiddleware

    gw_settings.gateway_max_concurrency = 1

    async def scenario():
        # 并发：实例内占槽/拒绝照常（进程内路径）
        mw = ConcurrencyLimitMiddleware(app=None)
        auth = [("authorization", "Bearer " + uuid.uuid4().hex)]
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(request: Request) -> JSONResponse:
            entered.set()
            await release.wait()
            return JSONResponse({"ok": True})

        async def fast(request: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        t = asyncio.create_task(mw.dispatch(Request(_scope(headers=auth)), slow))
        await entered.wait()
        r = await mw.dispatch(Request(_scope(headers=auth)), fast)
        assert r.status_code == 429
        release.set()
        assert (await t).status_code == 200

        # 幂等：实例内重放照常
        idem = IdempotencyMiddleware(app=None)
        calls = {"n": 0}

        async def call_next(request: Request) -> JSONResponse:
            calls["n"] += 1
            return JSONResponse({"exec": calls["n"]})

        key = uuid.uuid4().hex

        async def send():
            return await idem.dispatch(
                Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

        r1 = await send()
        r2 = await send()
        assert r2.headers["x-idempotent-replay"] == "true" and r2.body == r1.body
        assert calls["n"] == 1

    asyncio.run(scenario())
    # 熔断：跳闸/拒绝照常（进程内路径）
    clock = _FakeClock()
    br = CircuitBreaker(window_s=60, rate=0.5, min_samples=2, cooldown_s=30, clock=clock)
    m = "deg-" + uuid.uuid4().hex[:8]
    br.record_failure(m)
    br.record_failure(m)
    assert br.state(m) == 1 and not br.allow(m)

    for tag in ("concurrency", "idempotency", "circuit"):
        assert sum(1 for w in catcher.messages if tag in w) == 1, \
            f"{tag} 降级告警应恰好一条：{catcher.messages}"


# ---------- Redis 路径：两实例共享同一 Redis = 两副本 ----------

@requires_redis
def test_concurrency_shared_across_replicas(gw_settings, redis_url):
    """副本 A 占满共享并发额度后副本 B 被拒（429 判定基于 Redis 计数）；释放后 B 放行、键归零带 TTL。

    M54-A 时序加固：B 进场前先以探针轮询确认「A 的槽位已落 Redis 共享计数键」
    （entered 事件只证明 A 的 acquire 已返回，不能区分走 Redis 还是静默降级进程内）；
    释放后的归零断言同样改为 deadline 内轮询收敛。两处探针即反向自证：共享路径
    被旁路（真 bug）时键值到不了预期 → 显式红，断言语义未被放宽。
    """
    from eap.observability.gateway import ConcurrencyLimitMiddleware, credential_of

    gw_settings.gateway_max_concurrency = 1
    mw_a = ConcurrencyLimitMiddleware(app=None)
    mw_b = ConcurrencyLimitMiddleware(app=None)
    auth = [("authorization", "Bearer " + uuid.uuid4().hex)]  # 唯一凭证 → 唯一键
    key = f"eap:gw:conc:{credential_of(Request(_scope(headers=auth)))}"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = {"a": 0, "b": 0}

    async def slow_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        entered.set()
        await release.wait()
        return JSONResponse({"ok": True})

    async def fast_b(request: Request) -> JSONResponse:
        calls["b"] += 1
        return JSONResponse({"ok": True})

    async def scenario():
        t = asyncio.create_task(mw_a.dispatch(Request(_scope(headers=auth)), slow_a))
        await entered.wait()  # A 已完成 acquire（INCR 返回后才可能触达下游）
        val = _poll_shared_value(key, lambda v: v == "1")
        assert val == "1", \
            f"A 的槽位应已写入 Redis 共享计数键（共享路径被旁路/降级？）：{val!r}"
        r = await mw_b.dispatch(Request(_scope(headers=auth)), fast_b)
        assert r.status_code == 429, "副本 A 占满共享额度后副本 B 应被拒"
        assert r.headers["retry-after"] == "1"
        assert calls["b"] == 0 and mw_b._inflight == {}  # 未触达下游，且判定不走进程内 dict
        release.set()
        assert (await t).status_code == 200
        r2 = await mw_b.dispatch(Request(_scope(headers=auth)), fast_b)
        assert r2.status_code == 200  # A 释放（DECR）后 B 放行

    asyncio.run(scenario())

    val = _poll_shared_value(key, lambda v: v in (None, "0"))
    assert val in (None, "0"), f"在途计数应归零，实际 {val!r}"
    if val == "0":
        import redis as redis_sync

        r = redis_sync.from_url(redis_url, decode_responses=True)
        try:
            assert r.ttl(key) > 0  # TTL 防泄漏（崩溃残留计数自愈）
        finally:
            r.close()


@requires_redis
def test_concurrency_limit_is_global_not_per_replica(gw_settings, redis_url):
    """limit=2：A、B 各占 1 槽后，任一副本再来第 3 个请求被拒——全局额度而非每副本额度。

    M54-A：第 3 请求进场前探针轮询「共享计数 == 2」——两副本的槽位都真实落在
    Redis（而非任一实例静默降级进程内）才验证全局拒绝；降级时探针显式红。
    """
    from eap.observability.gateway import ConcurrencyLimitMiddleware, credential_of

    gw_settings.gateway_max_concurrency = 2
    mw_a = ConcurrencyLimitMiddleware(app=None)
    mw_b = ConcurrencyLimitMiddleware(app=None)
    auth = [("authorization", "Bearer " + uuid.uuid4().hex)]
    key = f"eap:gw:conc:{credential_of(Request(_scope(headers=auth)))}"
    both_entered = asyncio.Event()
    hold = asyncio.Event()
    n = {"in": 0}

    async def hold_on(request: Request) -> JSONResponse:
        n["in"] += 1
        if n["in"] == 2:
            both_entered.set()
        await hold.wait()
        return JSONResponse({"ok": True})

    async def scenario():
        ta = asyncio.create_task(mw_a.dispatch(Request(_scope(headers=auth)), hold_on))
        tb = asyncio.create_task(mw_b.dispatch(Request(_scope(headers=auth)), hold_on))
        await both_entered.wait()  # 两副本各占 1 槽，全局额度已满
        val = _poll_shared_value(key, lambda v: v == "2")
        assert val == "2", \
            f"两副本槽位应都落在 Redis 共享计数（共享路径被旁路/降级？）：{val!r}"
        r = await mw_a.dispatch(Request(_scope(headers=auth)), hold_on)
        assert r.status_code == 429
        hold.set()
        assert (await ta).status_code == 200 and (await tb).status_code == 200

    asyncio.run(scenario())
    assert n["in"] == 2  # 第 3 个请求未触达下游


@requires_redis
def test_breaker_state_shared_across_replicas(gw_settings, redis_url):
    """副本 A 跳闸 → 副本 B 同见 open 拒绝；冷却后探测 SETNX 跨副本单飞；探测成功全局 close。"""
    clock = _FakeClock()
    model = "gwr-" + uuid.uuid4().hex[:8]
    a = _breaker(clock)
    b = _breaker(clock)

    assert a.allow(model) and b.allow(model)
    for _ in range(4):
        a.record_failure(model)
    assert a.state(model) == 1
    assert b.state(model) == 1, "副本 B 应经 Redis 见到跳闸状态"
    assert not b.allow(model)

    clock.advance(31)
    assert b.state(model) == 2
    assert b.allow(model), "冷却后 B 抢到探测"
    assert not a.allow(model), "探测期间 A 不再抢到（跨副本 SETNX 单飞）"

    b.record_success(model)
    assert a.state(model) == 0 and b.state(model) == 0  # 探测成功 → 全局 close
    assert a.allow(model)


@requires_redis
def test_breaker_reopen_shared_and_keys_have_ttl(gw_settings, redis_url):
    """探测失败 → re-open 双副本可见（冷却重新计时）；状态/样本键均带 TTL 防永久泄漏。"""
    import redis as redis_sync

    clock = _FakeClock()
    model = "gwr2-" + uuid.uuid4().hex[:8]
    a = _breaker(clock, min_samples=2)
    b = _breaker(clock, min_samples=2)

    a.record_failure(model)
    r = redis_sync.from_url(redis_url, decode_responses=True)
    try:
        assert r.ttl(f"eap:gw:cb:{model}:fail") > 0  # 样本键 TTL
        a.record_failure(model)
        assert b.state(model) == 1
        assert r.ttl(f"eap:gw:cb:{model}:open") > 0  # 状态键 TTL

        clock.advance(31)
        assert b.allow(model)  # B 探测
        assert r.ttl(f"eap:gw:cb:{model}:probe") > 0  # 探测标记 TTL（崩溃自动释放）
        b.record_failure(model)  # 探测失败 → re-open
        assert a.state(model) == 1 and b.state(model) == 1
        clock.advance(10)
        assert a.state(model) == 1  # 自 re-open 起冷却未满
        clock.advance(21)
        assert a.state(model) == 2
    finally:
        r.close()


@requires_redis
def test_idempotency_replay_across_replicas(gw_settings, redis_url):
    """已完成响应跨副本重放：A 执行 → B 同键重放（X-Idempotent-Replay），B 下游不执行。"""
    from eap.observability.gateway import IdempotencyMiddleware

    mw_a = IdempotencyMiddleware(app=None)
    mw_b = IdempotencyMiddleware(app=None)
    key = uuid.uuid4().hex
    calls = {"a": 0, "b": 0}

    async def next_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        return JSONResponse({"exec": "a", "n": calls["a"]})

    async def next_b(request: Request) -> JSONResponse:
        calls["b"] += 1
        return JSONResponse({"exec": "b"})

    async def send(mw, call_next):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        r1 = await send(mw_a, next_a)
        assert r1.status_code == 200 and "x-idempotent-replay" not in r1.headers
        r2 = await send(mw_b, next_b)
        assert r2.headers["x-idempotent-replay"] == "true"
        assert r2.body == r1.body
        assert calls == {"a": 1, "b": 0}

    asyncio.run(scenario())


@requires_redis
def test_idempotency_inflight_cross_replica_waits_and_replays(gw_settings, monkeypatch, redis_url):
    """在途跨副本合并：A 慢执行中 B 同键到达 → SETNX 失败短轮询等 A 完成后重放；标记用后即删。"""
    import redis as redis_sync

    from eap.observability import gateway
    from eap.observability.gateway import IdempotencyMiddleware

    # 满载回归下 B 的 2s 轮询预算会被 CPU 拖爆（M48 实测）——放大到 10s，语义不变
    monkeypatch.setattr(gateway, "_IDEM_INFLIGHT_WAIT_S", 10.0)

    mw_a = IdempotencyMiddleware(app=None)
    mw_b = IdempotencyMiddleware(app=None)
    key = uuid.uuid4().hex
    calls = {"a": 0, "b": 0}
    a_started = asyncio.Event()

    async def slow_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        a_started.set()
        await asyncio.sleep(0.4)
        return JSONResponse({"exec": "a"})

    async def next_b(request: Request) -> JSONResponse:
        calls["b"] += 1
        return JSONResponse({"exec": "b"})

    async def send(mw, call_next):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        ta = asyncio.create_task(send(mw_a, slow_a))
        await a_started.wait()  # A 已抢到在途标记
        rb = await send(mw_b, next_b)  # B 轮询等待而非重复执行
        ra = await ta
        assert rb.headers["x-idempotent-replay"] == "true"
        assert rb.body == ra.body
        assert calls == {"a": 1, "b": 0}

    asyncio.run(scenario())
    r = redis_sync.from_url(redis_url, decode_responses=True)
    try:
        assert r.keys(f"eap:idem:inflight:*{key}") == []  # 执行完成后在途标记已清除
    finally:
        r.close()


@requires_redis
def test_idempotency_probe_failure_does_not_take_over(gw_settings, redis_url, monkeypatch):
    """M55：探测失败（None）不得视同「标记不存在」接管执行——重复执行副作用比
    等到 deadline 出 409 更违背幂等语义。反向自证：_redis_inflight_exists 抛异常时
    follower 轮询到 deadline → 409，next_b 永不执行（修复前会误接管 calls.b=1）。"""
    from eap.observability import gateway

    monkeypatch.setattr(gateway, "_IDEM_INFLIGHT_WAIT_S", 0.4)
    monkeypatch.setattr(gateway, "_IDEM_INFLIGHT_POLL_S", 0.03)
    mw_a = gateway.IdempotencyMiddleware(app=None)
    mw_b = gateway.IdempotencyMiddleware(app=None)
    key = uuid.uuid4().hex
    calls = {"a": 0, "b": 0}
    release = asyncio.Event()

    async def slow_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        await release.wait()
        return JSONResponse({"exec": "a"})

    async def next_b(request: Request) -> JSONResponse:
        calls["b"] += 1  # 修复前：探测异常被吞成「标记不存在」→ 误接管执行
        return JSONResponse({"exec": "b"})

    async def send(mw, call_next):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        ta = asyncio.create_task(send(mw_a, slow_a))
        assert await _poll_inflight_marked(redis_url, key), "A 应已抢到在途标记"
        # B 的 exists 探测全程失败（模拟 src 内部异常被吞后的 None 三态）——
        # fail-safe：不接管，等 deadline 409
        async def broken_exists(key_arg):
            return None  # None = 探测失败（src 层异常已吞；True/False 才是确定结论）
        monkeypatch.setattr(mw_b, "_redis_inflight_exists", broken_exists)
        rb = await send(mw_b, next_b)
        assert rb.status_code == 409, "探测失败应轮询到 deadline 而非误接管"
        release.set()
        await ta
        assert calls == {"a": 1, "b": 0}, "探测失败不得触发重复执行"

    asyncio.run(scenario())


@requires_redis
def test_idempotency_inflight_timeout_409_then_replay(gw_settings, redis_url, monkeypatch):
    """首次执行超过等待上限：后到者 409+Retry-After（宁可短暂拒绝不重复执行副作用）；完成后重试重放。

    M54-A：原「sleep(0.05) 假定 A 已抢到在途标记」在负载下是概率性前提（B 反抢
    标记 → B 执行 → 409 断言失效）；改为轮询等「标记可见于 Redis」这一共享态
    收敛后再放 B 进场——语义不变，前提由概率变确定。
    """
    from eap.observability import gateway

    monkeypatch.setattr(gateway, "_IDEM_INFLIGHT_WAIT_S", 0.2)
    monkeypatch.setattr(gateway, "_IDEM_INFLIGHT_POLL_S", 0.03)
    mw_a = gateway.IdempotencyMiddleware(app=None)
    mw_b = gateway.IdempotencyMiddleware(app=None)
    key = uuid.uuid4().hex
    release = asyncio.Event()
    calls = {"a": 0, "b": 0}

    async def slow_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        await release.wait()
        return JSONResponse({"exec": "a"})

    async def next_b(request: Request) -> JSONResponse:
        calls["b"] += 1
        return JSONResponse({"exec": "b"})

    async def send(mw, call_next):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        ta = asyncio.create_task(send(mw_a, slow_a))
        assert await _poll_inflight_marked(redis_url, key), \
            "A 应已抢到跨副本在途标记（SETNX）——超时说明共享路径未生效"
        rb = await send(mw_b, next_b)
        assert rb.status_code == 409
        assert rb.headers["retry-after"] == "1"
        assert calls["b"] == 0  # 409 未触达下游
        release.set()
        ra = await ta
        assert ra.status_code == 200
        r_after = await send(mw_b, next_b)  # 首次完成后重试 → 重放
        assert r_after.headers["x-idempotent-replay"] == "true"
        assert r_after.body == ra.body
        assert calls == {"a": 1, "b": 0}

    asyncio.run(scenario())


@requires_redis
def test_idempotency_inflight_failure_allows_takeover(gw_settings, redis_url):
    """首次执行失败清除在途标记：后到副本检测标记消失 → SETNX 接管自行执行（不 409、结果不丢）。"""
    from eap.observability.gateway import IdempotencyMiddleware

    mw_a = IdempotencyMiddleware(app=None)
    mw_b = IdempotencyMiddleware(app=None)
    key = uuid.uuid4().hex
    a_started = asyncio.Event()
    calls = {"a": 0, "b": 0}

    async def failing_a(request: Request) -> JSONResponse:
        calls["a"] += 1
        a_started.set()
        await asyncio.sleep(0.1)
        raise RuntimeError("boom")

    async def next_b(request: Request) -> JSONResponse:
        calls["b"] += 1
        return JSONResponse({"exec": "b"})

    async def send(mw, call_next):
        return await mw.dispatch(
            Request(_scope(path="/api/v1/x", headers=[("idempotency-key", key)])), call_next)

    async def scenario():
        ta = asyncio.create_task(send(mw_a, failing_a))
        await a_started.wait()
        tb = asyncio.create_task(send(mw_b, next_b))  # B 进入轮询等待
        with pytest.raises(RuntimeError):
            await ta  # A 失败，finally 清除在途标记
        rb = await tb
        assert rb.status_code == 200
        assert "x-idempotent-replay" not in rb.headers  # 接管执行而非重放
        assert calls == {"a": 1, "b": 1}

    asyncio.run(scenario())

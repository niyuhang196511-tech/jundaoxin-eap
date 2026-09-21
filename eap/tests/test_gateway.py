"""API Gateway 测试（M31 任务组 F）：并发上限 / 请求体上限 / 幂等键 / 熔断 / 超时。

全部离线确定性：中间件直接单测（asyncio.run 驱动，事件握手避免时序竞态）+
TestClient 集成各一；熔断用伪时钟注入。路由集成测试使用专属 capability
"gw-cb"/"gw-cb2"（不干扰其他用例的 chat 路由），结束清理 gw-* 模型。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from .conftest import AUTH


# ---------- 公共工具 ----------

@pytest.fixture()
def gateway_settings(monkeypatch):
    """网关配置复位：防护全部关闭（0），各用例按需打开（中间件请求时读取，patch 即生效）。"""
    from eap.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "gateway_max_concurrency", 0)
    monkeypatch.setattr(s, "gateway_max_body_bytes", 0)
    monkeypatch.setattr(s, "gateway_timeout_s", 0.0)
    monkeypatch.setattr(s, "gateway_idempotency_ttl_s", 300)
    return s


def _scope(method: str = "POST", path: str = "/x", headers: list[tuple[str, str]] | None = None) -> dict:
    """最小 ASGI scope（中间件直接单测用）。"""
    return {"type": "http", "asgi": {"version": "3.0"}, "method": method, "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
            "query_string": b"", "client": ("127.0.0.1", 1234)}


def _metrics_counter(series: str) -> float:
    from eap.observability.metrics import render

    for line in render().splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _gateway_app() -> FastAPI:
    """最小网关栈（注册顺序与 main.py 一致：LIFO 逆序 → 流经 Size→Concurrency→Idempotency）。"""
    from eap.observability.gateway import (ConcurrencyLimitMiddleware, IdempotencyMiddleware,
                                            RequestSizeLimitMiddleware)

    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return JSONResponse({"len": len(body)})

    return app


# ---------- 并发上限 ----------

def test_concurrency_middleware_unit(gateway_settings):
    """单测：limit=1 时第二请求 429+Retry-After；槽位 try/finally 释放后恢复放行；在途 gauge 透出。"""
    from eap.observability.gateway import ConcurrencyLimitMiddleware

    gateway_settings.gateway_max_concurrency = 1
    mw = ConcurrencyLimitMiddleware(app=None)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_call_next(request: Request) -> JSONResponse:
        calls["n"] += 1
        entered.set()
        await release.wait()
        return JSONResponse({"ok": True})

    async def scenario():
        auth = [("authorization", "Bearer dev-key-1")]  # 两请求同凭证 → 同一并发槽计数
        t1 = asyncio.create_task(mw.dispatch(Request(_scope(headers=auth)), slow_call_next))
        await entered.wait()  # 第一个请求已占槽
        assert _metrics_counter("eap_gateway_inflight") == 1
        r2 = await mw.dispatch(Request(_scope(headers=auth)), slow_call_next)
        assert r2.status_code == 429
        assert r2.headers["retry-after"] == "1"
        release.set()
        r1 = await t1
        assert r1.status_code == 200
        assert calls["n"] == 1  # 被拒请求未触达下游
        assert _metrics_counter("eap_gateway_inflight") == 0  # 槽位已释放
        r3 = await mw.dispatch(Request(_scope(headers=auth)), slow_call_next)
        assert r3.status_code == 200

    asyncio.run(scenario())
    assert _metrics_counter('eap_gateway_rejected_total{reason="concurrency"}') >= 1


def test_concurrency_limit_integration_429(gateway_settings):
    """TestClient 集成：慢端点占满并发槽 → 第二请求 429；释放后 200。"""
    gateway_settings.gateway_max_concurrency = 1
    app = _gateway_app()
    started, do_release = threading.Event(), threading.Event()

    @app.post("/slow")
    async def slow():
        started.set()
        await asyncio.to_thread(do_release.wait, 10)  # 阻塞至主线程放行（不占事件循环）
        return {"ok": True}

    with TestClient(app) as c1, TestClient(app) as c2:  # 两个客户端 = 两个并发入口，同一 app 实例
        box: dict = {}

        def first():
            box["r1"] = c1.post("/slow")

        t = threading.Thread(target=first)
        t.start()
        assert started.wait(10), "慢端点未开始执行"
        r2 = c2.post("/slow")
        assert r2.status_code == 429
        assert "retry-after" in r2.headers
        do_release.set()
        t.join(10)
        assert box["r1"].status_code == 200


def test_concurrency_disabled_by_default(gateway_settings):
    """limit=0（默认）不启用并发限制。"""
    app = _gateway_app()
    with TestClient(app) as c:
        assert c.post("/echo", json={"x": "1"}).status_code == 200


# ---------- 请求体上限 ----------

def test_body_size_limit_413(gateway_settings):
    """Content-Length 超限 → 413；小请求不受影响。"""
    gateway_settings.gateway_max_body_bytes = 16
    app = _gateway_app()
    with TestClient(app) as c:
        assert c.post("/echo", json={"x": "y"}).status_code == 200
        r = c.post("/echo", content=b"a" * 64, headers={"content-type": "application/octet-stream"})
        assert r.status_code == 413
        assert _metrics_counter('eap_gateway_rejected_total{reason="body_size"}') >= 1


def test_body_size_limit_disabled_by_default(gateway_settings):
    app = _gateway_app()
    with TestClient(app) as c:
        assert c.post("/echo", content=b"a" * 64,
                      headers={"content-type": "application/octet-stream"}).status_code == 200


# ---------- 幂等键 ----------

def test_idempotency_middleware_unit(gateway_settings):
    """单测：同键重放（带 X-Idempotent-Replay）、并发同键合并执行一次、TTL 过期重执行。"""
    from eap.observability.gateway import IdempotencyMiddleware

    mw = IdempotencyMiddleware(app=None)
    calls = {"n": 0}

    async def call_next(request: Request) -> JSONResponse:
        calls["n"] += 1
        await asyncio.sleep(0.01)  # 让并发同键真正重叠
        return JSONResponse({"exec": calls["n"]})

    async def send(key: str | None):
        headers = [("idempotency-key", key)] if key else []
        return await mw.dispatch(Request(_scope(path="/api/v1/memory", headers=headers)), call_next)

    async def scenario():
        # ① 首次执行：无重放标记
        r1 = await send("k-1")
        assert r1.status_code == 200 and "x-idempotent-replay" not in r1.headers
        # ② 同键重放：响应体一致 + 重放标记，下游不再执行
        r2 = await send("k-1")
        assert r2.headers["x-idempotent-replay"] == "true"
        assert r2.body == r1.body
        assert calls["n"] == 1
        # ③ 并发同键：合并为一次执行，等待方重放
        ra, rb = await asyncio.gather(send("k-2"), send("k-2"))
        assert calls["n"] == 2  # k-1 一次 + k-2 一次
        assert ra.body == rb.body
        assert sorted("x-idempotent-replay" in h.headers for h in (ra, rb)) == [False, True]
        # ④ 不同键各自执行
        await send("k-3")
        assert calls["n"] == 3
        # ⑤ TTL 过期：不重放，重新执行
        mw._store["ip:127.0.0.1:k-1"] = (time.monotonic() - 1, 200, b"old", "application/json")
        r5 = await send("k-1")
        assert "x-idempotent-replay" not in r5.headers and calls["n"] == 4

    asyncio.run(scenario())
    assert _metrics_counter("eap_idempotent_replays_total") >= 2  # ② 一次 + ③ 等待方一次


def test_idempotency_excludes_get_non_api_and_sse_paths(gateway_settings):
    """仅 /api/v1 写方法生效：GET、非 /api/v1、SSE 排除路径均直接放行。"""
    from eap.observability.gateway import IdempotencyMiddleware

    mw = IdempotencyMiddleware(app=None)
    calls = {"n": 0}

    async def call_next(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"exec": calls["n"]})

    async def scenario():
        h = [("idempotency-key", "k")]
        await mw.dispatch(Request(_scope(method="GET", path="/api/v1/memory", headers=h)), call_next)
        await mw.dispatch(Request(_scope(method="POST", path="/health", headers=h)), call_next)
        await mw.dispatch(Request(_scope(method="POST", path="/v1/chat/completions", headers=h)), call_next)
        await mw.dispatch(Request(_scope(method="POST", path="/a2a/rpc", headers=h)), call_next)
        assert calls["n"] == 4  # 全部直达下游，未进幂等通道

    asyncio.run(scenario())


def test_idempotent_memory_write_integration(client: TestClient):
    """集成（真实应用栈）：同 Idempotency-Key 写 memory 两次 → 第二次重放且只写一条。"""
    user = f"idem-{uuid.uuid4().hex[:8]}"
    base = len(client.get(f"/api/v1/memory?user_id={user}", headers=AUTH).json())
    key = uuid.uuid4().hex
    payload = {"scope": "user", "content": "幂等键测试记忆", "user_id": user}

    r1 = client.post("/api/v1/memory", headers={**AUTH, "Idempotency-Key": key}, json=payload)
    assert r1.status_code == 200, r1.text
    assert "x-idempotent-replay" not in r1.headers
    r2 = client.post("/api/v1/memory", headers={**AUTH, "Idempotency-Key": key}, json=payload)
    assert r2.status_code == 200
    assert r2.headers["x-idempotent-replay"] == "true"
    assert r2.json() == r1.json()  # 重放首次响应（含首次生成的记忆 id）
    after = len(client.get(f"/api/v1/memory?user_id={user}", headers=AUTH).json())
    assert after == base + 1, "服务端只应写入一条"


# ---------- 超时 ----------

def test_timeout_504_non_streaming(gateway_settings):
    """非流式请求超时 → 504（wait_for 掐断；计入 timeout 拒绝指标）。"""
    from eap.observability.gateway import ConcurrencyLimitMiddleware

    gateway_settings.gateway_timeout_s = 0.05
    mw = ConcurrencyLimitMiddleware(app=None)

    async def slow_call_next(request: Request) -> JSONResponse:
        await asyncio.sleep(1.0)
        return JSONResponse({"ok": True})  # pragma: no cover（不会到达）

    async def scenario():
        t0 = time.monotonic()
        resp = await mw.dispatch(Request(_scope()), slow_call_next)
        return resp, time.monotonic() - t0

    resp, elapsed = asyncio.run(scenario())
    assert resp.status_code == 504
    assert elapsed < 0.5, "超时应立即掐断"
    assert _metrics_counter('eap_gateway_rejected_total{reason="timeout"}') >= 1


def test_timeout_integration_skips_streaming_paths(gateway_settings):
    """集成：普通慢端点 504；SSE 排除路径（/v1/chat/completions、Accept: text/event-stream）豁免。"""
    gateway_settings.gateway_timeout_s = 0.05
    app = _gateway_app()

    @app.post("/slow")
    async def slow():
        await asyncio.sleep(0.3)
        return {"ok": True}

    @app.post("/v1/chat/completions")
    async def chat_like():
        await asyncio.sleep(0.2)  # 超过 0.05s 但路径在排除清单 → 不掐
        return {"ok": True}

    @app.get("/slow-accept")
    async def slow_accept():
        await asyncio.sleep(0.2)
        return {"ok": True}

    @app.get("/stream")
    async def stream():
        async def gen():
            await asyncio.sleep(0.2)
            yield "chunk"

        return StreamingResponse(gen(), media_type="text/event-stream")

    with TestClient(app) as c:
        assert c.post("/slow").status_code == 504
        assert c.post("/v1/chat/completions").status_code == 200  # 路径前缀豁免
        assert c.get("/slow-accept", headers={"accept": "text/event-stream"}).status_code == 200
        assert c.get("/stream").text == "chunk"  # 响应级流式（Accept 豁免）


# ---------- 熔断器 ----------

class _FakeClock:
    """伪时钟（注入 CircuitBreaker，确定性推进）。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _breaker(clock: _FakeClock):
    from eap.observability.gateway import CircuitBreaker

    return CircuitBreaker(window_s=60, rate=0.5, min_samples=4, cooldown_s=30, clock=clock)


def test_breaker_opens_half_open_recovers():
    """失败率超阈值 → open → 冷却后 half-open（单次探测）→ 成功 close。"""
    clock = _FakeClock()
    br = _breaker(clock)
    assert br.state("m") == 0 and br.allow("m")

    for _ in range(4):  # 4 连败 ≥ min_samples，失败率 1.0 ≥ 0.5
        br.record_failure("m")
    assert br.state("m") == 1  # open
    assert not br.allow("m")

    clock.advance(31)
    assert br.state("m") == 2  # half-open
    assert br.allow("m")  # 探测放行
    assert not br.allow("m")  # 探测期间单飞（其余仍拒绝）

    br.record_success("m")
    assert br.state("m") == 0  # close
    assert br.allow("m")


def test_breaker_reopens_on_probe_failure():
    """half-open 探测失败 → re-open（冷却重新计时）。"""
    clock = _FakeClock()
    br = _breaker(clock)
    for _ in range(4):
        br.record_failure("m")
    clock.advance(31)
    assert br.allow("m")  # half-open 探测
    br.record_failure("m")
    assert br.state("m") == 1  # re-open
    assert not br.allow("m")
    clock.advance(10)  # 不足冷却（自 re-open 起算）
    assert br.state("m") == 1
    clock.advance(21)
    assert br.state("m") == 2  # 冷却满再次半开


def test_breaker_window_expiry_and_rate_threshold():
    """滑动窗口：窗口外样本剪除不计；混合成败按窗口失败率判定。"""
    clock = _FakeClock()
    br = _breaker(clock)
    # 旧失败滑出窗口：窗口内仅 2 个样本 < min_samples → 不跳闸
    br.record_failure("m")
    br.record_failure("m")
    clock.advance(61)
    br.record_failure("m")
    br.record_failure("m")
    assert br.state("m") == 0
    # 成败混合：窗口内 2 败 + 2 成 = 失败率 0.5 ≥ rate → 跳闸
    br2 = _breaker(clock)
    br2.record_failure("x")
    br2.record_failure("x")
    br2.record_success("x")
    br2.record_success("x")
    assert br2.state("x") == 1
    # 成功为主不跳闸：1 败 + 3 成 = 0.25 < 0.5
    br3 = _breaker(clock)
    br3.record_failure("y")
    for _ in range(3):
        br3.record_success("y")
    assert br3.state("y") == 0


def test_breaker_metrics_exposed():
    """熔断指标：trips 计数 + state gauge（0/1/2）随状态透出。"""
    from eap.observability.metrics import render

    clock = _FakeClock()
    br = _breaker(clock)
    br.record_success("metric-model")
    assert 'eap_circuit_state{model="metric-model"} 0' in render()
    for _ in range(4):
        br.record_failure("metric-model")
    assert 'eap_circuit_trips_total{model="metric-model"}' in render()
    assert 'eap_circuit_state{model="metric-model"} 1' in render()


# ---------- 熔断接入模型路由（降级链联动） ----------

class _FlakyProvider:
    """必然失败的供应商适配（统计被路由到的次数）。"""

    def __init__(self, calls: dict) -> None:
        self._calls = calls

    async def complete(self, *, record, messages, tools, temperature, response_schema=None):
        from eap.modelhub.providers import ProviderError

        self._calls["flaky"] += 1
        raise ProviderError(f"{record.name}:boom")

    async def stream_complete(self, *, record, messages, temperature):
        from eap.modelhub.providers import ProviderError

        self._calls["flaky"] += 1
        raise ProviderError(f"{record.name}:boom")
        yield  # noqa: 让其成为异步生成器（迭代时才抛错，与 hub.stream 的消费方式一致）


class _GoodProvider:
    async def complete(self, *, record, messages, tools, temperature, response_schema=None):
        from eap.modelhub.providers import LLMResult

        return LLMResult(content="[gw-good] ok", tokens_in=1, tokens_out=1,
                         model=record.name, latency_ms=1)

    async def stream_complete(self, *, record, messages, temperature):
        yield "ok"


@pytest.fixture()
def gw_models(client: TestClient):
    """注册 gw-* 测试供应商与模型（专属 capability，结束清理，不污染其他用例）。"""
    from eap.db import SessionLocal
    from eap.modelhub.providers import register_provider
    from eap.models import ModelRecord

    calls = {"flaky": 0}
    register_provider("gw-flaky", _FlakyProvider(calls))
    register_provider("gw-good", _GoodProvider())
    with SessionLocal() as db:
        db.add(ModelRecord(name="gw-flaky-model", capabilities=["gw-cb"], provider="gw-flaky",
                           priority=1, enabled=True))
        db.add(ModelRecord(name="gw-flaky-solo", capabilities=["gw-cb2"], provider="gw-flaky",
                           priority=1, enabled=True))
        db.add(ModelRecord(name="gw-good-model", capabilities=["gw-cb"], provider="gw-good",
                           priority=2, enabled=True))
        db.commit()
    yield calls
    with SessionLocal() as db:  # 清理：避免残留模型影响同库的其他测试
        for name in ("gw-flaky-model", "gw-flaky-solo", "gw-good-model"):
            record = db.query(ModelRecord).filter_by(name=name).first()
            if record is not None:
                db.delete(record)
        db.commit()


def test_router_skips_tripped_model_to_fallback(client: TestClient, monkeypatch, gw_models):
    """熔断 open 的模型从降级链剔除：路由直接落到下一个模型，不再调用故障模型。"""
    from eap import observability
    from eap.db import SessionLocal
    from eap.modelhub.router import hub

    gateway = observability.gateway
    monkeypatch.setattr(gateway, "circuit_breaker",
                        gateway.CircuitBreaker(window_s=60, rate=0.5, min_samples=3, cooldown_s=60))
    msgs = [{"role": "user", "content": "熔断联动测试"}]

    with SessionLocal() as db:
        # 前 3 次：gw-flaky 失败 → 记入熔断 → 降级链落到 gw-good
        for _ in range(3):
            comp = asyncio.run(hub.complete(db, msgs, capability="gw-cb"))
            assert comp.record.name == "gw-good-model"
        assert gw_models["flaky"] == 3
        assert gateway.circuit_breaker.state("gw-flaky-model") == 1  # open

        # 熔断 open：路由过滤掉 gw-flaky → 不再调用，直接 gw-good
        comp = asyncio.run(hub.complete(db, msgs, capability="gw-cb"))
        assert comp.record.name == "gw-good-model"
        assert gw_models["flaky"] == 3, "熔断 open 后故障模型不应再被调用"
        assert gateway.circuit_breaker.state("gw-good-model") == 0  # 成功模型保持 closed


def test_router_all_models_open_raises(client: TestClient, monkeypatch, gw_models):
    """链上模型全部熔断 → 明确报错（不静默空链、不误报降级链耗尽）。"""
    from eap import observability
    from eap.db import SessionLocal
    from eap.modelhub.providers import ProviderError
    from eap.modelhub.router import hub

    gateway = observability.gateway
    breaker = gateway.CircuitBreaker(window_s=60, rate=0.5, min_samples=3, cooldown_s=60)
    monkeypatch.setattr(gateway, "circuit_breaker", breaker)
    for _ in range(3):  # 直接打满 gw-cb2 唯一模型的熔断窗口
        breaker.record_failure("gw-flaky-solo")
    assert breaker.state("gw-flaky-solo") == 1

    with SessionLocal() as db:
        with pytest.raises(ProviderError, match="熔断"):
            asyncio.run(hub.complete(db, [{"role": "user", "content": "x"}], capability="gw-cb2"))

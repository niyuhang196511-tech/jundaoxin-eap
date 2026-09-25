"""chunked（无 Content-Length）请求体网关策略测试（M55-B）。

EAP_GATEWAY_CHUNKED_MODE 两档：
- proxy（缺省）：chunked 请求直通——长度界定信任前置反代收口（现状向后兼容）；
- reject：Transfer-Encoding: chunked → 读体前 411 Length Required（EAP-411），
  且带 Content-Length 的请求行为不变（既有 413 语义回归）。

全部离线确定性、不触库（脏库可重入）：中间件 ASGI 直测（对齐 test_gateway.py 手法，
scope 最小构造）+ TestClient 集成各一组。TestClient 侧构造 chunked 的手法：httpx 客户端
总写 Content-Length，无法从客户端请求面直接构造 chunked——集成层套一个 ASGI 注入层
（剥 content-length、加 transfer-encoding: chunked 后透传真实栈），模拟 uvicorn 收到
chunked 请求时的 scope 形态：headers 带 transfer-encoding: chunked、无 content-length、
体以流式块到达（不依赖头帧）。

非法值边界：非法配置回落 proxy 并告警一次——对齐 config 既有惯例
（audit_export_limit 非法回落默认，见 tests/test_gateway_redis.py 的 _LogCatcher 注：
不用 caplog，模块级 logger 替身与全局日志配置解耦）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse


# ---------- 公共工具 ----------

@pytest.fixture()
def chunked_settings(monkeypatch):
    """网关配置复位：防护全关 + chunked 策略显式置位（中间件请求时读取，patch 即生效）。"""
    from eap.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "gateway_max_concurrency", 0)
    monkeypatch.setattr(s, "gateway_max_body_bytes", 0)
    monkeypatch.setattr(s, "gateway_timeout_s", 0.0)
    monkeypatch.setattr(s, "gateway_idempotency_ttl_s", 300)
    monkeypatch.setattr(s, "gateway_chunked_mode", "proxy")
    return s


@pytest.fixture(autouse=True)
def _reset_chunked_flags():
    """模块级去重标志按用例复位：契约启动日志 / 非法值告警均「每进程一次」。"""
    from eap.observability import gateway

    gateway._CHUNKED_CONTRACT_LOGGED = False
    gateway._CHUNKED_MODE_WARNED.clear()
    yield
    gateway._CHUNKED_CONTRACT_LOGGED = False
    gateway._CHUNKED_MODE_WARNED.clear()


def _scope(method: str = "POST", path: str = "/x", headers: list[tuple[str, str]] | None = None) -> dict:
    """最小 ASGI scope（中间件直接单测用，与 test_gateway.py 同款）。

    单测回声端点不读请求体（对齐既有手法：体完整性由 TestClient 集成用例覆盖，
    单测只断言放行/拒绝与下游触达）。
    """
    return {"type": "http", "asgi": {"version": "3.0"}, "method": method, "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
            "query_string": b"", "client": ("127.0.0.1", 1234)}


def _metrics_counter(series: str) -> float:
    from eap.observability.metrics import render

    for line in render().splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


class _ChunkedRewrite:
    """ASGI 注入层：剥 content-length、加 transfer-encoding: chunked 后透传真实栈。

    TestClient(httpx) 总写 Content-Length；本层把请求改写成 chunked 形态再进网关，
    等价于 uvicorn 收到 chunked 请求（体以 receive 流式块到达，不依赖头帧）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
            headers.append((b"transfer-encoding", b"chunked"))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def _chunked_app(calls: dict | None = None) -> FastAPI:
    """最小网关栈（注册顺序与 main.py 一致）+ 回声端点（计数触达，供「未触达下游」断言）。"""
    from eap.observability.gateway import ConcurrencyLimitMiddleware, IdempotencyMiddleware, \
        RequestSizeLimitMiddleware

    calls = calls if calls is not None else {"n": 0}
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.post("/echo")
    async def echo(request: Request):
        calls["n"] += 1
        body = await request.body()
        return JSONResponse({"len": len(body)})

    return app


class _LogCatcher:
    """替身 logger：截获 gateway 模块的 warning/info（不用 caplog，与全局日志配置解耦）。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg, *args) -> None:
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args) -> None:
        self.infos.append(msg % args if args else msg)


# ---------- proxy 模式（缺省）：chunked 直通 ----------

def test_proxy_mode_chunked_passes_unit(chunked_settings):
    """缺省 proxy：chunked 请求中间件层直通（现状语义），下游正常触达。"""
    from eap.observability.gateway import RequestSizeLimitMiddleware

    mw = RequestSizeLimitMiddleware(app=None)
    calls = {"n": 0}

    async def echo(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"ok": True})

    async def scenario():
        scope = _scope(headers=[("transfer-encoding", "chunked"),
                                ("authorization", "Bearer dev-key-1")])
        r = await mw.dispatch(Request(scope), echo)
        assert r.status_code == 200

    asyncio.run(scenario())
    assert calls["n"] == 1  # 直通且触达下游


def test_proxy_mode_chunked_passes_integration(chunked_settings):
    """TestClient 集成：chunked 形态请求 200 且体完整（len 与发送字节数一致）。"""
    app = _chunked_app()
    with TestClient(_ChunkedRewrite(app)) as c:
        r = c.post("/echo", content=b"hello-chunked",
                   headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200
    assert r.json() == {"len": len(b"hello-chunked")}


def test_proxy_mode_contract_logged_once(chunked_settings, monkeypatch):
    """启动日志写明运维契约：proxy 模式进程内恰好一条 info（不刷日志）。"""
    from eap.observability import gateway

    catcher = _LogCatcher()
    monkeypatch.setattr(gateway, "logger", catcher)
    app = _chunked_app()
    with TestClient(_ChunkedRewrite(app)) as c:
        assert c.post("/echo", content=b"x").status_code == 200
        assert c.post("/echo", content=b"y").status_code == 200  # 第二次请求不再打
    assert len(catcher.infos) == 1
    assert "client_max_body_size" in catcher.infos[0]  # 契约内容：前置反代收口


# ---------- reject 模式：读体前 411 ----------

def test_reject_mode_chunked_411_unit(chunked_settings):
    """reject：chunked 请求 → 411 Length Required + EAP-411；读体前拒绝（未触达下游）+ 计数。"""
    from eap.observability.gateway import RequestSizeLimitMiddleware

    chunked_settings.gateway_chunked_mode = "reject"
    mw = RequestSizeLimitMiddleware(app=None)
    calls = {"n": 0}

    async def echo(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"len": len(await request.body())})

    async def scenario():
        scope = _scope(headers=[("transfer-encoding", "chunked"),
                                ("authorization", "Bearer dev-key-1")])
        return await mw.dispatch(Request(scope), echo)

    r = asyncio.run(scenario())
    assert r.status_code == 411  # Length Required：缺长度界定（语义最准）
    assert "EAP-411" in r.body.decode()
    assert calls["n"] == 0  # 拒绝发生在读体之前：下游零触达（无慢速读体攻击面）
    assert _metrics_counter('eap_gateway_rejected_total{reason="chunked_body"}') >= 1


def test_reject_mode_chunked_411_integration(chunked_settings):
    """TestClient 集成：chunked 形态请求 411 + EAP-411，回声端点零触达。"""
    chunked_settings.gateway_chunked_mode = "reject"
    calls = {"n": 0}
    app = _chunked_app(calls)
    with TestClient(_ChunkedRewrite(app)) as c:
        r = c.post("/echo", content=b"hello-chunked",
                   headers={"content-type": "application/octet-stream"})
    assert r.status_code == 411
    assert "EAP-411" in r.json()["detail"]
    assert calls["n"] == 0  # 未触达下游


def test_reject_mode_te_and_cl_both_present_411(chunked_settings):
    """TE 与 Content-Length 同时出现：HTTP 语义 TE 优先（RFC 9112 §6.1）→ reject 下按 chunked 411。"""
    from eap.observability.gateway import RequestSizeLimitMiddleware

    chunked_settings.gateway_chunked_mode = "reject"
    mw = RequestSizeLimitMiddleware(app=None)

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def scenario():
        scope = _scope(headers=[("transfer-encoding", "chunked"), ("content-length", "4")])
        return await mw.dispatch(Request(scope), echo)

    assert asyncio.run(scenario()).status_code == 411


# ---------- 带 Content-Length 的请求：两种模式行为不变（既有 413 语义回归） ----------

def test_content_length_requests_unchanged_in_both_modes(chunked_settings):
    """CL 请求在 proxy/reject 两模式行为一致：限内 200、超限 413（既有语义回归）；无体 GET 不误伤。"""
    from eap.observability.gateway import RequestSizeLimitMiddleware

    chunked_settings.gateway_max_body_bytes = 16
    mw = RequestSizeLimitMiddleware(app=None)
    calls = {"n": 0}

    async def ok(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"ok": True})

    async def scenario():
        results = {}
        for mode in ("proxy", "reject"):
            chunked_settings.gateway_chunked_mode = mode
            results[mode] = [
                (await mw.dispatch(Request(_scope(method="GET", path="/echo")), ok)).status_code,
                (await mw.dispatch(Request(_scope(headers=[("content-length", "8")])), ok)).status_code,
                (await mw.dispatch(Request(_scope(headers=[("content-length", "64")])), ok)).status_code,
            ]
        return results

    results = asyncio.run(scenario())
    # 两模式三形态完全一致：无体 GET 放行（不误伤）、限内放行、超限 413
    assert results["proxy"] == results["reject"] == [200, 200, 413]
    assert _metrics_counter('eap_gateway_rejected_total{reason="body_size"}') >= 1
    assert calls["n"] == 4  # 413 各一次零触达：GET+小体（proxy/reject）共 4 次触达


def test_content_length_integration_unchanged(chunked_settings):
    """TestClient 集成回归：正常 CL 请求在 reject 模式不受影响（200），超限 413。"""
    chunked_settings.gateway_chunked_mode = "reject"
    chunked_settings.gateway_max_body_bytes = 16
    app = _chunked_app()
    with TestClient(app) as c:  # 不套 _ChunkedRewrite：httpx 原生 CL 请求
        assert c.post("/echo", content=b"12345678").status_code == 200
        r = c.post("/echo", content=b"a" * 64,
                   headers={"content-type": "application/octet-stream"})
        assert r.status_code == 413
        assert "EAP-413" in r.json()["detail"]


# ---------- 配置开关边界：非法值回落 proxy + 告警一次；大小写归一 ----------

def test_illegal_mode_falls_back_to_proxy_and_warns_once(chunked_settings, monkeypatch):
    """非法值（block）：回落 proxy（chunked 直通=现状）+ 告警恰好一次；重复请求不刷日志。"""
    from eap.observability import gateway

    chunked_settings.gateway_chunked_mode = "block"
    catcher = _LogCatcher()
    monkeypatch.setattr(gateway, "logger", catcher)
    mw = gateway.RequestSizeLimitMiddleware(app=None)
    calls = {"n": 0}

    async def echo(request: Request) -> JSONResponse:
        calls["n"] += 1
        return JSONResponse({"ok": True})

    async def scenario():
        scope = _scope(headers=[("transfer-encoding", "chunked")])
        for _ in range(3):
            await mw.dispatch(Request(scope), echo)

    asyncio.run(scenario())
    assert calls["n"] == 3  # 回落 proxy：chunked 直通（现状语义）
    assert len(catcher.warnings) == 1  # 告警一次不刷屏
    assert "EAP_GATEWAY_CHUNKED_MODE" in catcher.warnings[0] and "block" in catcher.warnings[0]


def test_mode_value_normalized(chunked_settings):
    """大小写/空白归一：'REJECT ' 等价 reject（生效 411）；空串视同未配置走 proxy。"""
    from eap.observability.gateway import RequestSizeLimitMiddleware

    mw = RequestSizeLimitMiddleware(app=None)

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def dispatch_chunked():
        scope = _scope(headers=[("transfer-encoding", "chunked")])
        return (await mw.dispatch(Request(scope), echo)).status_code

    chunked_settings.gateway_chunked_mode = "  REJECT "
    assert asyncio.run(dispatch_chunked()) == 411
    chunked_settings.gateway_chunked_mode = ""
    assert asyncio.run(dispatch_chunked()) == 200  # 空 = 未配置 = proxy 现状

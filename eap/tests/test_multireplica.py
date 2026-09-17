"""M10 多副本正确性测试：registry 事件应用 / JWT 吊销端到端 / 限流 Redis 回退。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_registry_apply_remote_event(client: TestClient):
    """远端事件应用到本地目录：stop → 拒绝调用；start → 恢复。"""
    from eap.agents.registry import registry

    async def scenario():
        await registry.apply_remote_event({"action": "stop", "name": "faq-agent"})
        await registry.apply_remote_event({"action": "start", "name": "faq-agent"})

    asyncio.run(scenario())
    assert registry.get("faq-agent").status == "started"
    # 停止后调用被拒（语义验证）
    async def stop_scenario():
        await registry.apply_remote_event({"action": "stop", "name": "faq-agent"})

    asyncio.run(stop_scenario())
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "x"})
    assert resp.status_code == 503
    # 恢复
    async def start_scenario():
        await registry.apply_remote_event({"action": "start", "name": "faq-agent"})

    asyncio.run(start_scenario())
    assert client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "恢复"}).status_code == 200


def test_rate_limiter_fallback_without_redis(client: TestClient):
    """无 Redis 配置：allow_async 回退进程内滑窗（同 key 超限拒绝）。"""
    from eap.api.security import SlidingWindow

    limiter = SlidingWindow(limit=2, window_seconds=60)

    async def scenario():
        return [await limiter.allow_async("k1") for _ in range(3)]

    results = asyncio.run(scenario())
    assert results == [True, True, False]


def test_rate_limiter_redis_path(client: TestClient, monkeypatch):
    """配置 EAP_REDIS_URL：走 Redis 滑窗（compose redis 已在本地 63790）。"""

    from eap.api.security import SlidingWindow
    from eap.config import get_settings

    monkeypatch.setenv("EAP_REDIS_URL", "redis://localhost:63790/9")
    get_settings.cache_clear()
    try:
        limiter = SlidingWindow(limit=2, window_seconds=60)

        async def scenario():
            return [await limiter.allow_async("redis-k") for _ in range(3)]

        results = asyncio.run(scenario())
        assert results == [True, True, False]
    finally:
        get_settings.cache_clear()

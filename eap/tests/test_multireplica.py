"""M10 多副本正确性测试：registry 事件应用 / JWT 吊销端到端 / 限流 Redis 回退。"""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def _redis_up() -> bool:
    """本地 63790（compose redis）可达性探测——不可达时 Redis 路径用例 skip 而非红。

    CI 教训（M51）：backend-postgres job 刻意不放 redis service，本用例曾因无守卫
    直连 63790 拒绝而红（该 job 的测试步骤在 lint 长期红时期从未执行，地雷未暴露）。
    """
    try:
        with socket.create_connection(("localhost", 63790), timeout=1):
            return True
    except OSError:
        return False


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


@pytest.mark.skipif(not _redis_up(), reason="本地 63790 无 Redis（compose redis 未起/CI 无 service）")
def test_rate_limiter_redis_path(client: TestClient, monkeypatch):
    """配置 EAP_REDIS_URL：走 Redis 滑窗（compose redis 已在本地 63790）。"""

    import redis as redis_lib

    from eap.api.security import SlidingWindow
    from eap.config import get_settings

    # 测试隔离：滑窗键持久在 Redis db9，跨运行会污染断言——先清空本用例专用库。
    # [T,T,F] 断言同时是滑窗成员唯一性的确定性守卫：M52 修复前成员为纯 f"{now}"，
    # Windows ~15.6ms 时钟刻度下三连发撞成员 → ZADD 覆盖不计数 → 第三发误放行（时运红绿）。
    redis_lib.Redis(host="localhost", port=63790, db=9).flushdb()
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

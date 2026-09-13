"""Redis Streams 任务队列集成测试（docs/03 §5 多副本后端）。

需要本机 Redis：默认 localhost:63790（compose 映射端口），可用 EAP_TEST_REDIS_URL 覆盖。
Redis 不可达时整文件 skip——离线回归不受影响。
"""

from __future__ import annotations

import os
import socket
import time

import pytest

from .conftest import AUTH

pytest.importorskip("redis", reason="redis 包未安装（uv sync --extra redis）")

REDIS_URL = os.environ.get("EAP_TEST_REDIS_URL", "redis://localhost:63790/5")


def _redis_alive() -> bool:
    try:
        with socket.create_connection(("localhost", 63790 if ":" not in REDIS_URL.split("//")[-1].split("/")[0] else REDIS_URL.split("//")[-1].split(":")[1].split("/")[0]), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alive(), reason="本机 Redis 不可达（docker compose up -d redis 启动）")


@pytest.fixture()
def redis_client_client():
    """带 EAP_REDIS_URL 的独立应用实例（进程内新引擎走 Redis Streams 后端）。"""
    import urllib.request

    host, port = "127.0.0.1", 63790
    with socket.create_connection((host, port), timeout=1):
        pass
    os.environ["EAP_REDIS_URL"] = f"redis://{host}:{port}/5"
    from eap.config import get_settings

    get_settings.cache_clear()
    from eap.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("EAP_REDIS_URL", None)
    get_settings.cache_clear()
    # 清理演示残留在 stream 的 key（db5 独立，不影响其他项目）
    import redis as redis_sync

    r = redis_sync.from_url(REDIS_URL, decode_responses=True)
    r.delete("eap:tasks")
    r.close()


def _wait_state(client, task_id, until, rounds=60):
    for _ in range(rounds):
        t = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if t["state"] in until:
            return t
        time.sleep(0.15)
    return t


def test_redis_stream_transport(client, redis_client_client):
    """任务经 Redis Streams 分发：提交后由消费者组取走执行，完成后 XACK。"""
    import redis as redis_sync

    r = redis_sync.from_url(REDIS_URL, decode_responses=True)
    before = r.xlen("eap:tasks")
    resp = redis_client_client.post("/api/v1/tasks", headers=AUTH,
                                    json={"type": "agent.invoke",
                                          "payload": {"agent": "faq-agent",
                                                      "input": "如何创建知识库？"}})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    t = _wait_state(redis_client_client, task_id, {"COMPLETED", "FAILED"})
    assert t["state"] == "COMPLETED", t
    assert "mock" in t["result"]["output"]
    # 已 XACK：stream 长度仍在增长但 pending 归零（消费组无滞留）
    pending = r.xpending("eap:tasks", "eap-workers")
    assert pending["pending"] == 0
    assert r.xlen("eap:tasks") >= before + 1


def test_redis_hitl_flow(client, redis_client_client):
    """HITL 经 Redis 后端：挂起 → 批准 → 重新入队（Redis）→ 续跑完成。"""
    resp = redis_client_client.post("/api/v1/tasks", headers=AUTH,
                                    json={"type": "agent.hitl",
                                          "payload": {"agent": "order-agent",
                                                      "input": "帮我下一台 EAP 一体机"}})
    task_id = resp.json()["task_id"]
    t = _wait_state(redis_client_client, task_id, {"WAITING_HUMAN", "COMPLETED", "FAILED"})
    assert t["state"] == "WAITING_HUMAN", t
    assert client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH,
                       json={"decision": True}).status_code == 200
    t = _wait_state(redis_client_client, task_id, {"COMPLETED", "FAILED"})
    assert t["state"] == "COMPLETED", t
    assert "SO-2026" in str(t["result"])

"""任务引擎 + HITL 审批测试：提交→执行→挂起→审批→续跑→取消。"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def _poll(client: TestClient, task_id: str, states: set[str], timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH)
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in states:
            return last
        time.sleep(0.15)
    return last


def test_async_agent_task(client: TestClient):
    """agent.invoke 长任务：入队 → RUNNING → COMPLETED。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "faq-agent", "input": "如何注册手写智能体？"}})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    task = _poll(client, task_id, {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert "mock" in task["result"]["output"]
    assert task["result"]["citations"]


def test_hitl_suspend_approve_resume(client: TestClient):
    """HITL 全流程：下单触发审批 → WAITING_HUMAN → 批准 → 续跑执行下单。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl",
                             "payload": {"agent": "order-agent", "input": "帮我下一台 EAP 一体机"}})
    task_id = resp.json()["task_id"]

    task = _poll(client, task_id, {"WAITING_HUMAN", "COMPLETED", "FAILED"})
    assert task["state"] == "WAITING_HUMAN", task["result"]
    assert task["pending_tool"] == "erp.order.create"
    snapshot_messages = task["result"]["messages"]
    assert snapshot_messages, "挂起时应保留消息快照（Checkpoint）"

    # 批准 → 续跑
    resp = client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH,
                       json={"decision": True, "comment": "价格已确认"})
    assert resp.status_code == 200

    task = _poll(client, task_id, {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert "SO-2026-" in task["result"]["output"], "批准后应真正执行下单工具"
    assert "否决" not in task["result"]["output"]


def test_hitl_deny(client: TestClient):
    """否决路径：工具不执行，模型收到否决说明。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl",
                             "payload": {"agent": "order-agent", "input": "下单测试拒绝路径"}})
    task_id = resp.json()["task_id"]
    task = _poll(client, task_id, {"WAITING_HUMAN", "COMPLETED", "FAILED"})
    assert task["state"] == "WAITING_HUMAN"

    resp = client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH,
                       json={"decision": False, "comment": "预算不足"})
    assert resp.status_code == 200
    task = _poll(client, task_id, {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED"
    assert "否决" in task["result"]["output"]
    assert "SO-2026-" not in task["result"]["output"], "否决后不得执行下单"


def test_cancel_pending_task(client: TestClient):
    """取消：注册一个慢处理器，提交后取消 → CANCELLED。"""
    import asyncio

    engine = client.app.state.task_engine

    async def slow_handler(payload, prev):
        await asyncio.sleep(5)
        return {"done": True}

    engine.register_handler("test.slow", slow_handler)
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "test.slow", "payload": {}})
    task_id = resp.json()["task_id"]

    # 提交后立即处于 PENDING/RUNNING（处理器睡眠 5s，窗口充足）
    task = _poll(client, task_id, {"RUNNING", "PENDING"}, timeout=3)
    assert task["state"] in ("RUNNING", "PENDING")

    resp = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=AUTH)
    assert resp.status_code == 200
    task = _poll(client, task_id, {"CANCELLED"})
    assert task["state"] == "CANCELLED"

    # 二次取消幂等
    resp = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=AUTH)
    assert resp.json()["state"] == "CANCELLED"


def test_approve_non_waiting_task_conflict(client: TestClient):
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "faq-agent", "input": "hi"}})
    task_id = resp.json()["task_id"]
    task = _poll(client, task_id, {"COMPLETED"})
    resp = client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH,
                       json={"decision": True})
    assert resp.status_code == 409

"""定时调度：周期任务自动提交 + 启停 + 删除（docs/03 §5）。

M52-D 可重入：调度名与任务 payload 标记加模块级 uuid 后缀——脏库重跑不撞
「调度 demo-cron 已存在」，且 COMPLETED 任务过滤只命中本运行自造任务（历史残留
同名标记任务不会让等待循环提前满足、last_run_at 尚未推进即断言）；模块级夹具
兜底删除自造调度（含修复前遗留的固定名残留——启用中的 1s 间隔调度若残留会
持续刷任务污染全库计数），失败路径也不再泄漏。
"""

from __future__ import annotations

import time
import uuid

import pytest

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

_SFX = uuid.uuid4().hex[:8]
DEMO_CRON = f"demo-cron-{_SFX}"
CRON_DAILY = f"cron-daily-{_SFX}"
SMOKE_INPUT = f"定时冒烟-{_SFX}"

# 修复前版本用固定名，失败中断时残留过启用中的 1s 调度（会持续刷任务）——启动即清
_LEGACY_NAMES = ("demo-cron", "cron-daily")


@pytest.fixture(scope="module", autouse=True)
def _cleanup_schedules(client):
    """前清历史固定名残留、后删本模块自造调度（防止启用中的周期调度跨运行刷任务）。"""
    for name in _LEGACY_NAMES:
        client.delete(f"/api/v1/tasks/schedules/{name}", headers=HEADERS)  # 404 无害
    yield
    for name in (DEMO_CRON, CRON_DAILY):
        client.delete(f"/api/v1/tasks/schedules/{name}", headers=HEADERS)  # 已删则 404 无害


def test_schedule_lifecycle(client):
    # 未知任务类型拒绝（404 不落库，固定坏名可保留）
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": "bad-type", "task_type": "no.such",
                          "payload": {}, "interval_seconds": 1})
    assert r.status_code == 404

    # 创建：每 1 秒跑一次 faq-agent 冒烟
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": DEMO_CRON, "task_type": "agent.invoke",
                          "payload": {"agent": "faq-agent", "input": SMOKE_INPUT},
                          "interval_seconds": 1, "note": "演示"})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    def _smoke_completed() -> list:
        tasks = client.get("/api/v1/tasks", headers=AUTH,
                           params={"state": "COMPLETED"}).json()
        return [t for t in tasks if t["payload"].get("input") == SMOKE_INPUT]

    # 等调度器跑 2-3 轮 → 产生已完成的周期任务（全量负载下引擎可能延迟：截止时间轮询，窗口 25s）
    deadline = time.time() + 25
    cron_tasks: list = []
    while time.time() < deadline:
        cron_tasks = _smoke_completed()
        if len(cron_tasks) >= 2:
            break
        time.sleep(0.5)
    assert len(cron_tasks) >= 2, len(cron_tasks)
    assert all(t["state"] == "COMPLETED" for t in cron_tasks)

    # next_run_at 被调度器推进
    sched = {s["name"]: s for s in client.get("/api/v1/tasks/schedules", headers=HEADERS).json()}
    assert sched[DEMO_CRON]["last_run_at"] is not None
    assert sched[DEMO_CRON]["next_run_at"] is not None

    # 停用 → 快照时间点后不再新增（先等在停用瞬间在途的任务到终态，快照才稳定）
    assert client.patch(f"/api/v1/tasks/schedules/{DEMO_CRON}?enabled=false",
                        headers=HEADERS).json()["enabled"] is False
    time.sleep(1.0)
    count_before = len(_smoke_completed())
    time.sleep(2.5)
    count_after = len(_smoke_completed())
    assert count_after == count_before

    # 重名 409 + 删除（首建名唯一化，409 用同名重复提交——M52-D 守则 3）
    assert client.post("/api/v1/tasks/schedules", headers=HEADERS,
                       json={"name": DEMO_CRON, "task_type": "agent.invoke",
                             "interval_seconds": 1}).status_code == 409
    assert client.delete(f"/api/v1/tasks/schedules/{DEMO_CRON}", headers=HEADERS).json()["status"] == "deleted"
    assert client.delete(f"/api/v1/tasks/schedules/{DEMO_CRON}", headers=HEADERS).status_code == 404


def test_schedule_cron_expression(client):
    """M17 cron 调度：表达式校验（无效 400）、next_run_at 按 cron 推算、_next_run 语义。"""
    # 无效 cron → 400（不落库，固定坏名可保留）
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": "cron-bad", "task_type": "agent.invoke",
                          "payload": {"agent": "faq-agent", "input": "x"},
                          "interval_seconds": 60, "cron": "not-a-cron"})
    assert r.status_code == 400, r.text

    # 有效 cron：每天零点（UTC）
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": CRON_DAILY, "task_type": "agent.invoke",
                          "payload": {"agent": "faq-agent", "input": "cron 冒烟"},
                          "interval_seconds": 86400, "cron": "0 0 * * *"})
    assert r.status_code == 200, r.text
    assert r.json()["cron"] == "0 0 * * *"

    sched = {s["name"]: s for s in client.get("/api/v1/tasks/schedules", headers=HEADERS).json()}
    assert sched[CRON_DAILY]["next_run_at"] is not None
    assert sched[CRON_DAILY]["next_run_at"].endswith("00:00:00"), sched[CRON_DAILY]

    # 清理
    assert client.delete(f"/api/v1/tasks/schedules/{CRON_DAILY}", headers=HEADERS).status_code == 200

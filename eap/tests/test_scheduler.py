"""定时调度：周期任务自动提交 + 启停 + 删除（docs/03 §5）。"""

from __future__ import annotations

import time

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def test_schedule_lifecycle(client):
    # 未知任务类型拒绝
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": "bad-type", "task_type": "no.such",
                          "payload": {}, "interval_seconds": 1})
    assert r.status_code == 404

    # 创建：每 1 秒跑一次 faq-agent 冒烟
    r = client.post("/api/v1/tasks/schedules", headers=HEADERS,
                    json={"name": "demo-cron", "task_type": "agent.invoke",
                          "payload": {"agent": "faq-agent", "input": "定时冒烟"},
                          "interval_seconds": 1, "note": "演示"})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    # 等调度器跑 2-3 轮 → 产生已完成的周期任务
    time.sleep(3)
    tasks = client.get("/api/v1/tasks", headers=AUTH,
                       params={"state": "COMPLETED"}).json()
    cron_tasks = [t for t in tasks if t["payload"].get("input") == "定时冒烟"]
    assert len(cron_tasks) >= 2, len(cron_tasks)
    assert all(t["state"] == "COMPLETED" for t in cron_tasks)

    # next_run_at 被调度器推进
    sched = {s["name"]: s for s in client.get("/api/v1/tasks/schedules", headers=HEADERS).json()}
    assert sched["demo-cron"]["last_run_at"] is not None
    assert sched["demo-cron"]["next_run_at"] is not None

    # 停用 → 快照时间点后不再新增
    assert client.patch("/api/v1/tasks/schedules/demo-cron?enabled=false",
                        headers=HEADERS).json()["enabled"] is False
    count_before = len([t for t in client.get("/api/v1/tasks", headers=AUTH,
                                              params={"state": "COMPLETED"}).json()
                        if t["payload"].get("input") == "定时冒烟"])
    time.sleep(2.5)
    count_after = len([t for t in client.get("/api/v1/tasks", headers=AUTH,
                                             params={"state": "COMPLETED"}).json()
                       if t["payload"].get("input") == "定时冒烟"])
    assert count_after == count_before

    # 重名 409 + 删除
    assert client.post("/api/v1/tasks/schedules", headers=HEADERS,
                       json={"name": "demo-cron", "task_type": "agent.invoke",
                             "interval_seconds": 1}).status_code == 409
    assert client.delete("/api/v1/tasks/schedules/demo-cron", headers=HEADERS).json()["status"] == "deleted"
    assert client.delete("/api/v1/tasks/schedules/demo-cron", headers=HEADERS).status_code == 404

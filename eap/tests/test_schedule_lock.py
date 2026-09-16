"""M8 调度锁测试：多副本（两个引擎实例）同轮调度，到期 schedule 只触发一次。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import TaskRecord, TaskScheduleRecord
from eap.runtime.tasks import TaskEngine


def test_schedule_claim_prevents_double_fire(client):
    """两个引擎的调度循环并发扫表：抢占式 next_run_at 更新保证只 submit 一次。"""
    from eap.config import get_settings

    calls: list[dict] = []

    async def handler(payload: dict, on_progress=None) -> dict:
        calls.append(payload)
        return {"ok": True}

    engine_a = TaskEngine()
    engine_b = TaskEngine()
    engine_a.register_handler("sched_test", handler)
    engine_b.register_handler("sched_test", handler)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        # 清理可能的历史残留
        for r in db.scalars(select(TaskScheduleRecord).where(
                TaskScheduleRecord.name == "m8-lock-test")).all():
            db.delete(r)
        for t in db.scalars(select(TaskRecord).where(
                TaskRecord.type == "sched_test")).all():
            db.delete(t)
        db.add(TaskScheduleRecord(
            name="m8-lock-test", task_type="sched_test", payload={"n": 1},
            interval_seconds=3600, enabled=True,
            next_run_at=now - timedelta(seconds=5)))
        db.commit()

    async def scenario():
        await engine_a.start(workers=1)
        await engine_b.start(workers=1)
        try:
            deadline = asyncio.get_running_loop().time() + 6
            while asyncio.get_running_loop().time() < deadline:
                if len(calls) >= 1:
                    break
                await asyncio.sleep(0.2)
            # 给第二个引擎留出"如果会重复触发"的窗口
            await asyncio.sleep(2.5)
        finally:
            await engine_a.stop()
            await engine_b.stop()

    asyncio.run(scenario())
    assert len(calls) == 1, f"调度被触发了 {len(calls)} 次（应为 1）"

    # 清理
    with SessionLocal() as db:
        for r in db.scalars(select(TaskScheduleRecord).where(
                TaskScheduleRecord.name == "m8-lock-test")).all():
            db.delete(r)
        for t in db.scalars(select(TaskRecord).where(
                TaskRecord.type == "sched_test")).all():
            db.delete(t)
        db.commit()

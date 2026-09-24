"""独立 Worker / 队列深化测试（M32 任务组 P1）：

- 租约：RUNNING 且租约过期 → 恢复扫描重置 PENDING 重跑；租约未过期不误恢复；
  终态/取消清租约；优雅停机回退在途任务
- 幂等键：同 key 在途任务命中返回同 id；终态后同 key 可再建（引擎级 + API 级）
- 优先级：asyncio 后端堆消费数值大优先、同级 FIFO
- Redis 双流：hi/lo 分流 + XAUTOCLAIM 接管（沿用 test_tasks_redis 的 skip 机制，
  本机无 Redis 自动跳过）
- worker 入口：import 冒烟（独立进程验证不拉起 eap.main，不起服务）

全部离线确定性（事件/显式控制，无 flaky 时序）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import AUTH

# Redis 用例 skip 机制（照抄 test_tasks_redis.py）：redis 包缺失整文件 skip，
# 本机 Redis 不可达仅跳过 Redis 用例
pytest.importorskip("redis", reason="redis 包未安装（uv sync --extra redis）")

REDIS_URL = os.environ.get("EAP_TEST_REDIS_URL", "redis://localhost:63790/6")


def _redis_alive() -> bool:
    try:
        with socket.create_connection(("localhost", 63790), timeout=1):
            return True
    except OSError:
        return False


# 仅装饰 Redis 用例（其余用例必须离线可跑）
redis_required = pytest.mark.skipif(
    not _redis_alive(), reason="本机 Redis 不可达（docker compose up -d redis 启动）")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _wait_task_state(task_id: str, states: set[str], timeout: float = 8.0) -> str:
    """同步轮询（仅供 API 用例：TestClient 的应用循环在独立 portal 线程）。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord

    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            state = db.get(TaskRecord, task_id).state
        if state in states:
            return state
        time.sleep(0.1)
    return state


async def _await_task_state(task_id: str, states: set[str], timeout: float = 8.0) -> str:
    """异步轮询：与被测引擎同事件循环时必须用 await 让出控制权（sync sleep 会饿死 worker）。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    state = ""
    while loop.time() < deadline:
        with SessionLocal() as db:
            state = db.get(TaskRecord, task_id).state
        if state in states:
            return state
        await asyncio.sleep(0.1)
    return state


# ---------- 租约机制 ----------

def test_lease_scan_recovers_expired_running_task(client):
    """RUNNING + 过期租约：引擎启动扫描 → 重置 PENDING 重新入队 → 再执行成功。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import AsyncioQueueBackend, TaskEngine

    async def echo(payload: dict, prev: dict) -> dict:
        return {"echo": payload.get("text", "")}

    # 同进程「第二引擎」共享 DB 模拟双副本；echo 也注册到应用引擎——
    # 无论哪个副本的扫描抢先命中（抢占式 UPDATE），任务都能被正确执行
    client.app.state.task_engine.register_handler("echo", echo)
    engine = TaskEngine(AsyncioQueueBackend())
    engine.register_handler("echo", echo)

    with SessionLocal() as db:
        db.add(TaskRecord(id="lease-exp-1", type="echo", state="RUNNING",
                          payload={"text": "hi"}, lease_expires_at=_now() - timedelta(seconds=10)))
        db.commit()

    async def scenario():
        await engine.start(workers=1)  # 启动即跑租约恢复扫描（无需等 60s 周期）
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                with SessionLocal() as db:
                    task = db.get(TaskRecord, "lease-exp-1")
                    if task.state == "COMPLETED":
                        return task
                await asyncio.sleep(0.1)
            return None
        finally:
            await engine.stop()

    task = asyncio.run(scenario())
    assert task is not None and task.state == "COMPLETED", "过期租约任务应被恢复重跑"
    with SessionLocal() as db:
        assert db.get(TaskRecord, "lease-exp-1").lease_expires_at is None  # 终态清租约


def test_lease_scan_leaves_running_task_with_valid_lease(client):
    """正常执行中（租约未过期）：恢复扫描不重置；执行租约取任务时已置。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import AsyncioQueueBackend, TaskEngine

    started = asyncio.Event()
    release = asyncio.Event()

    async def gate(payload: dict, prev: dict) -> dict:
        started.set()
        await release.wait()  # 测试显式放行（确定性，无 sleep 时序）
        return {"ok": True}

    engine = TaskEngine(AsyncioQueueBackend())
    engine.register_handler("test.lease", gate)  # 独有类型：避免其他用例残留任务被启动恢复误接

    async def scenario():
        await engine.start(workers=1)
        try:
            before = _now()
            with SessionLocal() as db:
                submitted = await engine.submit(db, "test.lease", {})
            task_id = str(submitted)
            await asyncio.wait_for(started.wait(), timeout=5)
            with SessionLocal() as db:
                task = db.get(TaskRecord, task_id)
                assert task.state == "RUNNING"
                assert task.lease_expires_at is not None, "取任务执行前应置租约"
                assert task.lease_expires_at > before, "租约应指向未来（now + lease_seconds）"
            assert await engine._recover_leases() == 0, "租约未过期不得恢复"
            with SessionLocal() as db:
                assert db.get(TaskRecord, task_id).state == "RUNNING"
            release.set()
            assert await _await_task_state(task_id, {"COMPLETED"}) == "COMPLETED"
        finally:
            release.set()  # 兜底放行，避免引擎 stop 卡在在途任务
            await engine.stop()

    asyncio.run(scenario())


def test_graceful_stop_requeues_inflight_task(client):
    """engine 正常 stop：在途任务回退 PENDING 并清租约（先清租约再退出，不误判崩溃）。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import AsyncioQueueBackend, TaskEngine

    started = asyncio.Event()

    async def gate(payload: dict, prev: dict) -> dict:
        started.set()
        await asyncio.sleep(60)  # 由停机取消打断

    engine = TaskEngine(AsyncioQueueBackend())
    engine.register_handler("test.gate", gate)

    async def scenario():
        await engine.start(workers=1)
        try:
            with SessionLocal() as db:
                submitted = await engine.submit(db, "test.gate", {})
            task_id = str(submitted)
            await asyncio.wait_for(started.wait(), timeout=5)
            return task_id
        finally:
            await engine.stop()  # 在途任务被取消 → 停机分支回退 PENDING 并清租约

    task_id = asyncio.run(scenario())
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        assert task.state == "PENDING", f"在途任务应回退 PENDING，实际 {task.state}"
        assert task.lease_expires_at is None, "停机回退应清租约"
        task.state = "CANCELLED"  # 清理：置终态，避免残留 PENDING 被后续引擎启动恢复干扰
        db.commit()


def test_drain_waits_for_inflight_then_finishes(client):
    """drain（独立 worker 优雅停第一阶段）：停领新任务、等在途完成，任务不丢。"""
    from eap.db import SessionLocal
    from eap.runtime.tasks import AsyncioQueueBackend, TaskEngine

    started = asyncio.Event()
    release = asyncio.Event()

    async def gate(payload: dict, prev: dict) -> dict:
        started.set()
        await release.wait()
        return {"ok": True}

    engine = TaskEngine(AsyncioQueueBackend())
    engine.register_handler("test.drain", gate)  # 独有类型：避免其他用例残留任务被启动恢复误接

    async def scenario():
        await engine.start(workers=1)
        try:
            with SessionLocal() as db:
                submitted = await engine.submit(db, "test.drain", {})
            task_id = str(submitted)
            await asyncio.wait_for(started.wait(), timeout=5)
            drain_task = asyncio.create_task(engine.drain(timeout=5))
            await asyncio.sleep(0.05)  # 让 drain 进入等待循环
            assert not drain_task.done(), "在途未完成，drain 应继续等待"
            release.set()
            await asyncio.wait_for(drain_task, timeout=5)
            assert engine._running == {}
            await engine.stop()
            assert await _await_task_state(task_id, {"COMPLETED"}) == "COMPLETED"
        finally:
            release.set()
            await engine.stop()

    asyncio.run(scenario())


# ---------- 任务幂等键 ----------

def test_idempotency_key_dedup_and_rebuild(client):
    """幂等键（引擎级）：同 key 并发 submit 返回同 id；终态后同 key 可再建。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import AsyncioQueueBackend, TaskEngine

    engine = TaskEngine(AsyncioQueueBackend())  # 未 start：任务保持 PENDING（在途可命中）

    async def submit_with_session(task_type: str, key: str | None):
        with SessionLocal() as db:
            return await engine.submit(db, task_type, {"text": key}, idempotency_key=key)

    async def scenario():
        first = await submit_with_session("echo", "idem-k1")
        assert first.existing is False
        # 并发提交同 key：命中同一在途任务
        second, third = await asyncio.gather(
            submit_with_session("echo", "idem-k1"),
            submit_with_session("echo", "idem-k1"))
        assert str(second) == str(first) == str(third), "同 key 在途任务应命中同一 id"
        assert second.existing and third.existing

        # 终态后同 key 可再建
        with SessionLocal() as db:
            task = db.get(TaskRecord, str(first))
            task.state = "COMPLETED"
            task.lease_expires_at = None
            db.commit()
        fourth = await submit_with_session("echo", "idem-k1")
        assert str(fourth) != str(first)
        assert fourth.existing is False

    asyncio.run(scenario())


def test_api_idempotency_key_and_priority(client):
    """API：body 字段 idempotency_key 幂等命中（同 task_id + existing）；priority 透传落库。

    HTTP 层的 Idempotency-Key 头归 M31 网关幂等中间件（同键重放），任务级去重走 body
    字段——两层语义互补，避免头名冲突被网关重放遮蔽。
    """
    from eap.db import SessionLocal
    from eap.models import TaskRecord

    async def slow(payload: dict, prev: dict) -> dict:
        await asyncio.sleep(30)  # 保证两次提交间任务在途（由取消收尾）

    client.app.state.task_engine.register_handler("test.gate", slow)

    r1 = client.post("/api/v1/tasks", headers=AUTH,
                     json={"type": "test.gate", "payload": {}, "priority": 7,
                           "idempotency_key": "api-idem-1"})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["existing"] is False

    r2 = client.post("/api/v1/tasks", headers=AUTH,
                     json={"type": "test.gate", "payload": {}, "priority": 7,
                           "idempotency_key": "api-idem-1"})
    body2 = r2.json()
    assert body2["task_id"] == body1["task_id"], "同 key 应命中同一任务"
    assert body2["existing"] is True

    with SessionLocal() as db:
        task = db.get(TaskRecord, body1["task_id"])
        assert task.priority == 7, "priority 应透传落库"
        assert task.idempotency_key == "api-idem-1"

    # 置终态（取消在途慢任务）→ 同 key 可再建
    assert client.post(f"/api/v1/tasks/{body1['task_id']}/cancel",
                       headers=AUTH).status_code == 200
    assert _wait_task_state(body1["task_id"], {"CANCELLED"}) == "CANCELLED"

    r3 = client.post("/api/v1/tasks", headers=AUTH,
                     json={"type": "test.gate", "payload": {},
                           "idempotency_key": "api-idem-1"})
    body3 = r3.json()
    assert body3["existing"] is False
    assert body3["task_id"] != body1["task_id"]
    # 收尾：取消第二个慢任务，释放 worker
    client.post(f"/api/v1/tasks/{body3['task_id']}/cancel", headers=AUTH)
    _wait_task_state(body3["task_id"], {"CANCELLED"})


# ---------- 队列优先级 ----------

def test_asyncio_backend_priority_order(client):
    """asyncio 后端堆消费：数值大优先、同级 FIFO。依序入队 low/high1/high2 →
    出队 high1 → high2 → low。"""
    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import AsyncioQueueBackend

    with SessionLocal() as db:
        db.add_all([
            TaskRecord(id="prio-low", type="echo", state="PENDING", payload={}, priority=0),
            TaskRecord(id="prio-high-1", type="echo", state="PENDING", payload={}, priority=5),
            TaskRecord(id="prio-high-2", type="echo", state="PENDING", payload={}, priority=5),
        ])
        db.commit()

    async def scenario():
        backend = AsyncioQueueBackend()
        await backend.start()
        try:
            # priority 缺省：由后端从 TaskRecord 读取
            await backend.enqueue("prio-low")
            await backend.enqueue("prio-high-1")
            await backend.enqueue("prio-high-2")
            order = [(await backend.get())[0] for _ in range(3)]
            assert order == ["prio-high-1", "prio-high-2", "prio-low"], order
        finally:
            await backend.stop()

    asyncio.run(scenario())


# ---------- Redis 双流（hi/lo 分流 + XAUTOCLAIM 接管） ----------

@redis_required
def test_redis_dual_stream_priority_and_reclaim(client):
    """Redis 双流：priority>0 入 hi 流先被消费；实例崩溃遗留 pending 被
    XAUTOCLAIM 接管重投且保留优先级。"""
    import redis as redis_sync

    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.runtime.tasks import RedisStreamBackend

    r = redis_sync.from_url(REDIS_URL, decode_responses=True)
    r.delete("eap:tasks:hi", "eap:tasks")  # db6 测试键清理（含消费组）
    try:
        with SessionLocal() as db:
            db.add_all([
                TaskRecord(id="redis-lo-1", type="echo", state="PENDING", payload={}, priority=0),
                TaskRecord(id="redis-hi-1", type="echo", state="PENDING", payload={}, priority=5),
            ])
            db.commit()

        async def scenario():
            # 副本 A：消费两条但不 ACK（模拟处理中崩溃，pending 遗留）
            backend_a = RedisStreamBackend(REDIS_URL, min_idle_ms=60_000)
            await backend_a.start()
            await backend_a.enqueue("redis-lo-1")   # priority=0 → lo 流
            await backend_a.enqueue("redis-hi-1")   # priority=5 → hi 流
            got = [(await backend_a.get())[0] for _ in range(2)]
            assert got == ["redis-hi-1", "redis-lo-1"], f"hi 流应先被消费: {got}"
            await backend_a.stop()

            await asyncio.sleep(0.3)  # pending 空闲超过副本 B 的接管阈值
            # 副本 B：启动即 XAUTOCLAIM 接管重投（保留优先级）
            backend_b = RedisStreamBackend(REDIS_URL, min_idle_ms=100)
            await backend_b.start()
            try:
                recovered = []
                for _ in range(2):
                    task_id, receipt = await backend_b.get()
                    recovered.append(task_id)
                    await backend_b.ack(receipt)
                assert recovered == ["redis-hi-1", "redis-lo-1"], \
                    f"接管重投应保留优先级（hi 在先）: {recovered}"
            finally:
                await backend_b.stop()

        asyncio.run(scenario())
    finally:
        r.delete("eap:tasks:hi", "eap:tasks")
        r.close()


# ---------- worker 入口 ----------

def test_worker_entry_import_smoke():
    """worker 入口冒烟：独立进程 import eap.worker 成功且不拉起 eap.main（不起服务）。"""
    code = ("import eap.worker, sys; "
            "assert 'eap.main' not in sys.modules, 'worker 不得导入 eap.main'; "
            "assert callable(eap.worker.main)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=60, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert proc.returncode == 0, proc.stderr


def test_worker_settings_defaults():
    """worker 配置项：EAP_WORKER_COUNT / EAP_WORKER_LEASE_SECONDS 默认值。"""
    from eap.config import get_settings

    s = get_settings()
    assert s.worker_count == 2
    assert s.worker_lease_seconds == 300

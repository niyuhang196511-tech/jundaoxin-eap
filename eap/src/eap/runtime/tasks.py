"""Task/Job 引擎：8 态状态机的 M2 子集（docs/03 §5）。

- asyncio 队列 + Worker 池（单实例开发版；多副本换 Redis Streams，接口不变）
- 处理器注册表：按任务类型分发
- HITL：TaskSuspended → WAITING_HUMAN，快照落库，审批后续跑
- 取消：排队中直接标记；运行中经 asyncio 取消传播
- M32 生产化（任务组 P1）：执行租约（RUNNING 且租约过期 → 重置 PENDING 重跑，
  worker 崩溃兜底）、队列优先级（数值大优先、同级 FIFO）、任务幂等键（同 key
  在途任务命中直接返回）、优雅停机（drain 等在途完成 / 停机回退清租约）
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import TaskRecord
from .loop import TaskSuspended
from .interaction import InteractionRequested

if TYPE_CHECKING:
    from datetime import datetime

Handler = Callable[[dict, dict], Awaitable[dict]]

# 幂等键命中的「在途」状态：终态（COMPLETED/FAILED/CANCELLED）不命中，同 key 可再建
_ACTIVE_STATES = ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT")


class TaskSubmitResult(str):
    """submit() 返回值：值即 task_id（str 子类，既有按字符串消费的调用方零改动），
    附 .existing 标记是否命中同幂等键的在途任务（True=未新建，返回既有任务）。"""

    existing: bool

    def __new__(cls, task_id: str, existing: bool = False) -> "TaskSubmitResult":
        obj = super().__new__(cls, task_id)
        obj.existing = existing
        return obj


def _task_priority(task_id: str) -> int:
    """读 TaskRecord.priority（数值大优先）；任务不存在/查询失败回退 0。"""
    try:
        from ..models import TaskRecord as _TaskRecord

        with SessionLocal() as db:
            task = db.get(_TaskRecord, task_id)
            return int(task.priority or 0) if task is not None else 0
    except Exception:
        return 0


@dataclass
class TaskSnapshot:
    """挂起快照：审批续跑所需的全部状态。"""

    messages: list[dict]
    pending_tool: str
    pending_args: str
    approvals: dict


# ---------- 队列后端（docs/03 §5：单实例 asyncio 队列 / 多副本 Redis Streams，接口一致） ----------
# 队列语义（M32）：priority 数值大优先；同优先级 FIFO（按入队序）。

class AsyncioQueueBackend:
    """单实例开发版后端：进程内优先级堆（不持久、不跨实例）。

    消费用 heapq 堆（键 (-priority, seq, task_id)）：数值大优先，同级按 seq（入队序）
    FIFO。priority 从 TaskRecord.priority 读取（enqueue 未显式给 priority 时查库，缺省 0）。
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, str]] = []  # (-priority, seq, task_id)
        self._seq = 0  # 同优先级 FIFO 序（单调递增，保证堆排序稳定）
        self._cond: asyncio.Condition | None = None

    async def start(self) -> None:
        self._heap = []
        self._seq = 0
        self._cond = asyncio.Condition()

    async def stop(self) -> None:
        self._heap = []
        self._cond = None

    async def enqueue(self, task_id: str, priority: int | None = None) -> None:
        """入队；priority 为 None 时从 TaskRecord.priority 读取（与 Redis 后端同语义）。"""
        cond = self._cond
        if cond is None:
            return  # 后端未启动/已停止：静默丢弃（与单流时代行为一致）
        if priority is None:
            priority = _task_priority(task_id)
        heapq.heappush(self._heap, (-int(priority), self._seq, task_id))
        self._seq += 1
        async with cond:
            cond.notify_all()

    async def get(self) -> tuple[str, str]:
        """返回 (task_id, receipt)；asyncio 后端 receipt 即 task_id。最高优先级先出，同级 FIFO。"""
        cond = self._cond
        if cond is None:
            raise RuntimeError("AsyncioQueueBackend 未启动")
        async with cond:
            while not self._heap:
                await cond.wait()
            _neg_priority, _seq, task_id = heapq.heappop(self._heap)
        return task_id, task_id

    async def ack(self, receipt: str) -> None:
        pass  # 堆消费无 pending 概念；保留接口与 Redis 后端对齐


class RedisStreamBackend:
    """多副本后端（docs/03 §5）：Redis Streams + 消费组，双流优先级（M32）。

    - 双流：{stream}:hi（priority > 0）与 {stream}（默认 lo 流，复用基础 stream 名——
      priority=0 的行为与单流时代完全一致，兼容既有消费端/监控）
    - 队列语义：数值大优先（>0 入 hi 流、先被消费）；同级 FIFO（流内按 Redis 序）
    - 提交 XADD；消费 XREADGROUP（先 hi 短 block 再 lo，高优先级尽快领取）；完成 XACK
    - 启动时 XAUTOCLAIM 接管空闲超阈值的 pending 条目（两条流都做，重投保留原优先级）——
      前一实例崩溃遗留的任务自动重投
    - 消费者名含随机后缀：每个副本独立身份
    """

    STREAM = "eap:tasks"        # lo 流（默认优先级，复用基础 stream 名）
    HI_STREAM = "eap:tasks:hi"  # hi 流（priority > 0）
    GROUP = "eap-workers"
    HI_BLOCK_MS = 1_000  # hi 流短 block：高优先级任务尽快被领取
    LO_BLOCK_MS = 5_000  # lo 流 block：空转轮询周期，同时限定 hi 任务被 lo 消费阻塞的上限

    def __init__(self, url: str, min_idle_ms: int = 60_000) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)
        self._consumer = f"worker-{uuid.uuid4().hex[:8]}"
        self._reclaimer: asyncio.Task | None = None
        self._min_idle_ms = min_idle_ms  # XAUTOCLAIM 最小空闲毫秒（生产 60s；测试可调小）

    async def start(self) -> None:
        for stream in (self.HI_STREAM, self.STREAM):
            try:
                await self._r.xgroup_create(stream, self.GROUP, id="0", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise
        # 崩溃恢复：接管其他实例遗留的 pending（空闲超阈值，hi/lo 两流）
        await self._reclaim_stale()
        # 运行期周期接管（M8）：某实例处理中崩溃后，存活实例自动接管其 pending 消息
        self._reclaimer = asyncio.create_task(self._reclaim_loop())

    async def _reclaim_stale(self) -> int:
        """XAUTOCLAIM 接管空闲超阈值的 pending → 按原优先级重新入队（幂等：XACK 原消息）。"""
        recovered = 0
        for stream in (self.HI_STREAM, self.STREAM):
            try:
                claimed = await self._r.xautoclaim(stream, self.GROUP, self._consumer,
                                                   min_idle_time=self._min_idle_ms, count=20)
                messages = claimed[1] if isinstance(claimed, (tuple, list)) else []
                for entry in messages:
                    msg_id, fields = (entry[0], entry[1]) if isinstance(entry, tuple) else (entry, {})
                    task_id = fields.get("task_id") if isinstance(fields, dict) else None
                    if not task_id:
                        continue
                    await self._xadd(task_id, int(fields.get("priority") or 0))
                    await self._r.xack(stream, self.GROUP, msg_id)
                    recovered += 1
            except Exception:
                continue  # 单流恢复失败不阻塞启动/运行（Redis 兼容实现差异）
        if recovered:
            logging.getLogger("eap.tasks").info("接管 %d 条遗留 pending 消息", recovered)
        return recovered

    async def _reclaim_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self._reclaim_stale()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    async def stop(self) -> None:
        if self._reclaimer is not None:
            self._reclaimer.cancel()
            try:
                await self._reclaimer
            except asyncio.CancelledError:
                pass
            self._reclaimer = None
        await self._r.aclose()

    async def _xadd(self, task_id: str, priority: int) -> None:
        """按优先级选流写入；priority 同时落 fields（接管重投时保留）。"""
        fields: dict[str, str] = {"task_id": task_id}
        if priority:
            fields["priority"] = str(priority)
        stream = self.HI_STREAM if priority > 0 else self.STREAM
        await self._r.xadd(stream, fields)

    async def enqueue(self, task_id: str, priority: int | None = None) -> None:
        """入队；priority 为 None 时从 TaskRecord.priority 读取（>0 入 hi 流）。"""
        if priority is None:
            priority = _task_priority(task_id)
        await self._xadd(task_id, int(priority))

    async def get(self) -> tuple[str, str]:
        """先 hi 短 block 再 lo：高优先级优先消费，同流内 FIFO。"""
        while True:
            for stream, block in ((self.HI_STREAM, self.HI_BLOCK_MS),
                                  (self.STREAM, self.LO_BLOCK_MS)):
                resp = await self._r.xreadgroup(self.GROUP, self._consumer,
                                                {stream: ">"}, count=1, block=block)
                for _s, messages in resp or []:
                    for msg_id, fields in messages:
                        task_id = fields.get("task_id")
                        if task_id:
                            return task_id, msg_id

    async def ack(self, receipt: str) -> None:
        await self._r.xack(self.STREAM, self.GROUP, receipt)
        await self._r.xack(self.HI_STREAM, self.GROUP, receipt)


class TaskEngine:
    def __init__(self, queue_backend=None) -> None:
        self.handlers: dict[str, Handler] = {}
        self._backend = queue_backend
        self._workers: list[asyncio.Task] = []
        self._scheduler: asyncio.Task | None = None
        self._lease_scanner: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self._stopping = False  # 停机标记：停止领取新任务；在途任务被取消时回退 PENDING 清租约

    def register_handler(self, task_type: str, handler: Handler) -> None:
        self.handlers[task_type] = handler

    # ---------- 生命周期 ----------

    async def start(self, workers: int = 2) -> None:
        if self._backend is None:
            from ..config import get_settings

            settings = get_settings()
            if settings.redis_url:
                self._backend = RedisStreamBackend(settings.redis_url)
            else:
                self._backend = AsyncioQueueBackend()
        await self._backend.start()
        print(f"[tasks] 引擎启动：{type(self._backend).__name__} × {workers} workers")
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(workers)]
        self._scheduler = asyncio.create_task(self._schedule_loop())
        self._lease_scanner = asyncio.create_task(self._lease_loop())
        await self._recover_pending()
        await self._recover_leases()  # 崩溃 worker 泄漏的 RUNNING（租约过期）同样兜底恢复

    async def _recover_pending(self) -> None:
        """崩溃恢复（M7）：把重启前遗留在 PENDING 的任务重新入队。

        AsyncioQueueBackend 不持久——进程退出即丢队列内容，但 TaskRecord 已落库（PENDING）；
        不恢复则任务永远滞留。RUNNING/等待审批的快照态不在恢复范围（由 HITL/超时语义管辖）。
        恢复范围：updated_at 早于本引擎启动时刻的 PENDING——排除本进程并发写入的记录，
        也避免多测试/多引擎实例互相捞取对方刚提交的任务。
        """
        from datetime import datetime, timezone

        from sqlalchemy import select

        from ..db import SessionLocal
        from ..models import TaskRecord

        boot_at = datetime.now(timezone.utc).replace(tzinfo=None)
        with SessionLocal() as db:
            pending = db.scalars(
                select(TaskRecord)
                .where(TaskRecord.state == "PENDING",
                       TaskRecord.updated_at < boot_at)
                .limit(100)).all()
            ids = [t.id for t in pending]
        for task_id in ids:
            await self.enqueue(task_id)
        if ids:
            logging.getLogger("eap.tasks").info("恢复 %d 个遗留 PENDING 任务: %s", len(ids), ids)

    async def stop(self) -> None:
        """停机：先置停机标记再取消 worker——在途任务经停机分支回退 PENDING 并清租约
        （不遗留 RUNNING+租约被恢复扫描误判为 worker 崩溃），然后停调度/租约扫描/后端。"""
        self._stopping = True
        for w in self._workers:
            w.cancel()
        if self._scheduler is not None:
            self._scheduler.cancel()
        if self._lease_scanner is not None:
            self._lease_scanner.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        if self._scheduler is not None:
            await asyncio.gather(self._scheduler, return_exceptions=True)
        if self._lease_scanner is not None:
            await asyncio.gather(self._lease_scanner, return_exceptions=True)
        self._workers = []
        self._scheduler = None
        self._lease_scanner = None
        if self._backend is not None:
            await self._backend.stop()

    async def drain(self, timeout: float = 30.0) -> None:
        """优雅停机第一阶段（独立 worker 进程用）：停止领取新任务，等待在途任务完成。

        超时即返回——未完成的在途任务由随后的 stop() 取消路径回退 PENDING，不丢失。
        """
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._running and loop.time() < deadline:
            await asyncio.sleep(0.05)

    # ---------- 提交 / 查询 ----------

    async def submit(self, db: Session, task_type: str, payload: dict,
                     priority: int = 0, idempotency_key: str | None = None) -> TaskSubmitResult:
        """提交任务，返回 TaskSubmitResult（str 子类：值即 task_id，.existing 标记幂等命中）。

        幂等语义（M32）：提供 idempotency_key 时，若存在同 key 且在途
        （PENDING / RUNNING / WAITING_HUMAN / WAITING_INPUT）的任务 → 直接返回该任务 id
        （existing=True，不新建）；任务到终态（COMPLETED/FAILED/CANCELLED）后同 key 可再建。
        返回值选 str 子类而非 tuple：既有调用方（kb/evals/triggers）把返回值当 task_id
        字符串用，零改动兼容；需区分命中时读 .existing。
        并发窗口：幂等检查与插入非原子（无部分唯一索引），单实例内同步 Session 串行执行
        无窗口；多副本对同 key 真并发提交理论上可能各建一条（at-least-once 语义可接受）。
        """
        if idempotency_key:
            existing = db.scalar(
                select(TaskRecord)
                .where(TaskRecord.idempotency_key == idempotency_key,
                       TaskRecord.state.in_(_ACTIVE_STATES))
                .limit(1))
            if existing is not None:
                return TaskSubmitResult(existing.id, existing=True)
        task_id = uuid.uuid4().hex
        record = TaskRecord(id=task_id, type=task_type, state="PENDING", payload=payload,
                            priority=int(priority), idempotency_key=idempotency_key)
        db.add(record)
        db.commit()
        await self.enqueue(task_id, priority=int(priority))
        return TaskSubmitResult(task_id, existing=False)

    async def enqueue(self, task_id: str, priority: int | None = None) -> None:
        """入队（提交/审批续跑/恢复重跑共用）；priority None 时由后端从 TaskRecord 读取。"""
        await self._backend.enqueue(task_id, priority=priority)

    async def cancel(self, task_id: str) -> str:
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if task.state in ("COMPLETED", "CANCELLED"):
                return task.state
            if task.state == "RUNNING" and task_id in self._running:
                self._cancel_requested.add(task_id)
                self._running[task_id].cancel()
                return "CANCELLING"
            task.state = "CANCELLED"
            task.lease_expires_at = None  # 取消即终态：清租约
            db.commit()
            return "CANCELLED"

    async def approve(self, task_id: str, decision: bool, comment: str = "") -> str:
        """HITL 审批：合并决定到快照 → PENDING 重新入队续跑。"""
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if task.state != "WAITING_HUMAN":
                raise ValueError(f"任务 {task_id} 状态为 {task.state}，不在等待人工审批")
            result = dict(task.result or {})
            pending = result.get("pending", {})
            approvals = dict(result.get("approvals", {}))
            approvals[pending.get("tool", "")] = bool(decision)
            result["approvals"] = approvals
            result.setdefault("approvals_log", []).append(
                {"tool": pending.get("tool"), "decision": bool(decision), "comment": comment})
            task.result = result
            task.state = "PENDING"
            db.commit()
        await self.enqueue(task_id)
        return "PENDING"

    async def interact(self, task_id: str, values: dict) -> str:
        """交互引擎（v0.5-④）：合并用户提交的表单值到快照 → PENDING 重新入队续跑。

        提交值写入 result.interactions[key]，handler 恢复时经 resume 快照回传给 agent。
        """
        if not isinstance(values, dict) or not values:
            raise ValueError("values 必须是非空对象")
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if task.state != "WAITING_INPUT":
                raise ValueError(f"任务 {task_id} 状态为 {task.state}，不在等待用户输入")
            result = dict(task.result or {})
            pending = result.get("pending_interaction", {})
            interactions = dict(result.get("interactions", {}))
            interactions[pending.get("key", "input")] = values
            result["interactions"] = interactions
            result["pending_interaction"] = None
            task.result = result
            task.state = "PENDING"
            db.commit()
        await self.enqueue(task_id)
        return "PENDING"

    # ---------- 定时调度（docs/03 §5）：到期即 submit，走统一队列/HITL/取消链路 ----------

    @staticmethod
    def _next_run(s) -> "datetime":
        """下次执行时刻：cron 优先（croniter 从当前推算），否则 interval_seconds。

        cron 解析失败回退 interval（调度不因坏表达式停摆，告警可见）。
        """
        from datetime import datetime, timedelta, timezone

        if s.cron:
            try:
                from croniter import croniter

                base = datetime.now(timezone.utc)
                return croniter(s.cron, base).get_next(datetime).replace(tzinfo=None)
            except Exception as e:
                logging.getLogger("eap.tasks").warning(
                    "调度 %s cron %r 无效（回退 interval）: %s", s.name, s.cron, e)
                return datetime.now(timezone.utc) + timedelta(seconds=s.interval_seconds or 60)
        return datetime.now(timezone.utc) + timedelta(seconds=s.interval_seconds or 60)

    async def _schedule_loop(self) -> None:
        from datetime import datetime, timezone

        from ..models import TaskScheduleRecord

        while True:
            try:
                now = datetime.now(timezone.utc)
                with SessionLocal() as db:
                    due = db.scalars(select(TaskScheduleRecord).where(
                        TaskScheduleRecord.enabled == True,  # noqa: E712
                        TaskScheduleRecord.next_run_at <= now)).all()
                    for s in due:
                        # 多副本防重复触发（M8）：抢占式更新 next_run_at——
                        # UPDATE ... WHERE next_run_at=<旧值> 命中 0 行说明另一副本已抢到
                        claimed = db.execute(
                            update(TaskScheduleRecord)
                            .where(TaskScheduleRecord.id == s.id,
                                   TaskScheduleRecord.next_run_at == s.next_run_at)
                            .values(next_run_at=self._next_run(s), last_run_at=now)
                        ).rowcount
                        if claimed:
                            await self.submit(db, s.task_type, dict(s.payload or {}))
                    db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.getLogger("eap.tasks").warning("调度循环异常: %s", e)
            await asyncio.sleep(1)

    # ---------- 租约恢复（M32）：worker 崩溃兜底——RUNNING 且租约过期 → PENDING 重跑 ----------

    async def _recover_leases(self) -> int:
        """恢复扫描：重置「RUNNING 且 lease_expires_at < now」的任务为 PENDING 并重新入队。

        `_recover_pending` 管重启前遗留的 PENDING；本扫描管执行中 worker 失联泄漏的
        RUNNING（租约到期未达终态）。抢占式重置（UPDATE ... WHERE 租约=<旧值>）保证
        多副本引擎并发扫描只有一个命中，不重复入队。任务实际仍在执行的极端情形
        （执行时长超过租约且未续约）会重复执行——at-least-once 语义，处理器需可重入。
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recovered: list[str] = []
        with SessionLocal() as db:
            stale = db.scalars(
                select(TaskRecord)
                .where(TaskRecord.state == "RUNNING",
                       TaskRecord.lease_expires_at < now)
                .limit(100)).all()
            for task in stale:
                claimed = db.execute(
                    update(TaskRecord)
                    .where(TaskRecord.id == task.id,
                           TaskRecord.state == "RUNNING",
                           TaskRecord.lease_expires_at == task.lease_expires_at)
                    .values(state="PENDING", lease_expires_at=None)
                ).rowcount
                if claimed:
                    recovered.append(task.id)
            db.commit()
        for task_id in recovered:
            await self.enqueue(task_id)
        if recovered:
            logging.getLogger("eap.tasks").warning(
                "租约过期，恢复 %d 个泄漏 RUNNING 任务重新入队: %s", len(recovered), recovered)
        return len(recovered)

    async def _lease_loop(self) -> None:
        """租约恢复周期扫描（60s 级）：回收崩溃 worker 泄漏的 RUNNING 任务。"""
        while True:
            try:
                await asyncio.sleep(60)
                await self._recover_leases()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.getLogger("eap.tasks").warning("租约恢复扫描异常: %s", e)

    def _requeue_interrupted(self, task_id: str) -> None:
        """停机回退：在途 RUNNING 任务 → PENDING 并清租约（优雅停机不误触发恢复扫描，
        下次启动经 _recover_pending 重跑，多副本场景由存活实例立即拾起）。"""
        try:
            with SessionLocal() as db:
                task = db.get(TaskRecord, task_id)
                if task is not None and task.state == "RUNNING":
                    task.state = "PENDING"
                    task.lease_expires_at = None
                    db.commit()
                    logging.getLogger("eap.tasks").info("停机回退在途任务 %s → PENDING", task_id)
        except Exception as e:
            logging.getLogger("eap.tasks").warning("停机回退任务 %s 失败: %s", task_id, e)

    # ---------- 执行 ----------

    async def _worker(self, i: int) -> None:
        while not self._stopping:  # 停机后不再领取新任务（在途任务经 drain/取消收尾）
            try:
                task_id, receipt = await self._backend.get()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Redis 抖动等瞬态错误：不杀 worker，记录后重试
                print(f"[tasks] worker{i} 取任务失败: {e}")
                await asyncio.sleep(1)
                continue
            try:
                await self._run_one(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 处理器之外的意外错误
                self._mark(task_id, "FAILED", {"error": str(e)})
            finally:
                try:
                    await self._backend.ack(receipt)
                except Exception as e:
                    print(f"[tasks] worker{i} ack 失败: {e}")

    async def _run_one(self, task_id: str) -> None:
        from ..observability.tracing import enabled as otel_enabled, tracer

        span_cm = None
        if otel_enabled():
            # trace 传播：提交方经 payload._trace_id 透传（跨进程无上下文头，用属性关联）
            with SessionLocal() as db:
                _payload = db.get(TaskRecord, task_id)
                submit_trace = (_payload.payload or {}).get("_trace_id", "") if _payload else ""
            span_cm = tracer().start_as_current_span(f"task.run {task_id}")
            span_cm.__enter__()
            span = tracer().get_current_span()
            if span is not None:
                span.set_attribute("eap.task_id", task_id)
                if submit_trace:
                    span.set_attribute("eap.submit_trace_id", submit_trace)
        try:
            await self._run_one_inner(task_id)
        except Exception as e:
            if span_cm is not None:
                span = tracer().get_current_span()
                if span is not None:
                    span.record_exception(e)
                span_cm.__exit__(type(e), e, e.__traceback__)
            raise
        else:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    async def _run_one_inner(self, task_id: str) -> None:
        from datetime import datetime, timedelta, timezone

        from ..config import get_settings

        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None or task.state not in ("PENDING",):
                return
            task.state = "RUNNING"
            # 执行租约（M32）：取任务时置 now + EAP_WORKER_LEASE_SECONDS；
            # 到期未达终态 → 恢复扫描判 worker 崩溃，重置 PENDING 重跑
            task.lease_expires_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                                     + timedelta(seconds=get_settings().worker_lease_seconds))
            db.commit()
            payload = dict(task.payload or {})
            prev_result = dict(task.result or {})
            task_type = task.type

        handler = self.handlers.get(task_type)
        if handler is None:
            self._mark(task_id, "FAILED", {"error": f"未知任务类型 {task_type}"})
            return

        runner = asyncio.current_task()
        assert runner is not None
        self._running[task_id] = runner
        try:
            result = await handler(payload, prev_result)
        except TaskSuspended as sus:
            self._mark(task_id, "WAITING_HUMAN", {
                "messages": sus.messages,
                "pending": {"tool": sus.pending_tool, "arguments": sus.pending_args},
                "approvals": prev_result.get("approvals", {}),
            })
            # M39-B（docs/18 §二.5 移动远程审批）：挂起即推 IM 审批卡片到手机。
            # 仅 agent.hitl/agent.invoke 任务生效；懒 import 防循环依赖（模式同
            # _mark 的 emit_event）；推送/入队失败只告警，不影响挂起状态。
            if task_type in ("agent.hitl", "agent.invoke"):
                try:
                    from .im_outbound import notify_hitl

                    with SessionLocal() as ndb:
                        await notify_hitl(task_id, str(payload.get("agent") or ""),
                                          sus.pending_tool, ndb)
                except Exception as e:
                    logging.getLogger("eap.tasks").warning(
                        "HITL 审批卡片推送失败 task=%s: %s", task_id, e)
            return
        except InteractionRequested as ir:
            # 交互引擎（v0.5-④）：结构化输入挂起 → WAITING_INPUT + 快照，interact 端点提交后续跑
            self._mark(task_id, "WAITING_INPUT", {
                "messages": ir.messages,
                "pending_interaction": {"key": ir.request.key, "title": ir.request.title,
                                        "description": ir.request.description,
                                        "schema": ir.request.schema},
                "interactions": (prev_result or {}).get("interactions", {}),
            })
            return
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                self._mark(task_id, "CANCELLED", {"error": "被人工取消"})
                return
            if self._stopping:
                # 引擎正常停机（stop/drain 取消 worker）：在途任务回退 PENDING 并清租约，
                # 不遗留 RUNNING+租约被恢复扫描误判为 worker 崩溃
                self._requeue_interrupted(task_id)
                return
            raise
        except Exception as e:
            self._mark(task_id, "FAILED", {"error": str(e)})
            await self._notify_done(task_id, task_type, "FAILED", {"error": str(e)})
            return
        finally:
            self._running.pop(task_id, None)
            self._cancel_requested.discard(task_id)

        self._mark(task_id, "COMPLETED", result)
        await self._notify_done(task_id, task_type, "COMPLETED", result)

    async def _notify_done(self, task_id: str, task_type: str, state: str,
                           result: dict) -> None:
        """M40-A（docs/18 §二.5「完成后卡片回执结果」）：任务终态 → IM 完成回执卡片。

        仅 agent.invoke/agent.hitl 任务类型生效（docs/18 §二.5 移动远程操作范畴）；
        懒 import 防循环依赖（模式同挂起点 notify_hitl 钩子）；summary 取
        result.output（失败取 error，卡片内 ≤200 字符截断）；推送/入队失败只告警，
        不影响任务引擎终态落库（非阻断）。
        """
        if task_type not in ("agent.invoke", "agent.hitl"):
            return
        try:
            from .im_outbound import notify_task_done

            data = result or {}
            summary = str(data.get("output") or data.get("error") or "")
            with SessionLocal() as ndb:
                await notify_task_done(task_id, task_type, state, summary, ndb)
        except Exception as e:
            logging.getLogger("eap.tasks").warning(
                "任务完成回执推送失败 task=%s: %s", task_id, e)

    def _mark(self, task_id: str, state: str, result: dict) -> None:
        from ..observability.metrics import incr

        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                return
            task.state = state
            task.result = result
            if state != "RUNNING":
                task.lease_expires_at = None  # 终态/挂起即清租约（挂起任务不占租约）
            db.commit()
        try:
            incr("eap_tasks_total", {"state": state})
        except Exception:
            pass
        # 事件中心（M30）：任务终态事件（COMPLETED/FAILED），发射失败不阻断任务引擎
        if state in ("COMPLETED", "FAILED"):
            try:
                from .events import emit_event

                emit_event(f"task.{'completed' if state == 'COMPLETED' else 'failed'}",
                           data={"task_id": task_id, "type": task.type, "state": state,
                                 "error": (result or {}).get("error", "")})
            except Exception:
                pass

    # ---------- 内置处理器 ----------

    async def _h_kb_ingest(self, payload: dict, prev_result: dict) -> dict:
        """异步文档摄入（M12/M14）：解析后端可指定（local / mineru_*），解析+分块+嵌入不阻塞请求。"""
        from sqlalchemy import select

        from ..knowledge import service as kb_svc
        from ..knowledge.parsers import extract_text
        from ..models import KB, Chunk

        kb_name = str(payload.get("kb", ""))
        title = str(payload.get("title", "未命名"))
        content = str(payload.get("text", ""))
        if payload.get("file_b64"):
            import base64

            content = extract_text(str(payload.get("filename", "doc.txt")),
                                   base64.b64decode(payload["file_b64"]),
                                   parser=payload.get("parser") or None)
        if not content.strip():
            return {"status": "failed", "error": "解析后内容为空"}

        with SessionLocal() as db:
            kb = db.scalar(select(KB).where(KB.name == kb_name))
            if kb is None:
                return {"status": "failed", "error": f"知识库 {kb_name} 不存在"}
            doc = kb_svc.ingest_text(db, kb, title, content,
                                     source=str(payload.get("source", "upload")),
                                     meta={"parser": payload.get("parser") or "",
                                           "original_filename": payload.get("filename") or "",
                                           "original_file_b64": payload.get("file_b64") or ""})
            chunk_count = len(db.scalars(
                select(Chunk.id).where(Chunk.doc_id == doc.id)).all())
            return {"status": "ok", "document_id": doc.id, "title": title,
                    "chunks": chunk_count}

    async def _h_eval_run(self, payload: dict, prev_result: dict) -> dict:
        """评测后台执行（v0.6-③④）：agent 数据集走问答评测；rag 数据集走检索指标。"""
        from sqlalchemy import select as _select

        from ..knowledge import service as kb_svc
        from ..models import KB, EvalDatasetRecord, EvalRunRecord
        from .rag_eval import aggregate, evaluate_retrieval

        run_id = payload["run_id"]
        dataset_name = payload["dataset"]
        top_k = int(payload.get("top_k") or 5)
        min_rate = float(payload.get("min_pass_rate") or 0.8)
        judge = payload.get("judge") or "rule"

        with SessionLocal() as db:
            run = db.get(EvalRunRecord, run_id)
            dataset = db.scalar(_select(EvalDatasetRecord)
                                .where(EvalDatasetRecord.name == dataset_name))
            if run is None or dataset is None:
                return {"status": "failed", "error": "运行或数据集不存在"}
            # 模型直评（M42-B）：payload.model 非空 → 用例逐条经 hub prefer=model 调用
            if payload.get("model"):
                from ..api.v1.evals import execute_model_evaluation

                result = await execute_model_evaluation(db, str(payload["model"]), dataset_name,
                                                        min_rate, judge)
                run = db.get(EvalRunRecord, run_id)
                run.verdict = result["verdict"]
                run.pass_rate = result["pass_rate"]
                run.scores = result["scores"]
                db.commit()
                return {"status": "ok", "run_id": run_id, "verdict": result["verdict"]}
            if dataset.kind != "rag":
                from ..api.v1.evals import execute_evaluation

                result = await execute_evaluation(db, payload["agent"], dataset_name,
                                                  min_rate, judge)
                run = db.get(EvalRunRecord, run_id)
                run.verdict = result["verdict"]
                run.pass_rate = result["pass_rate"]
                run.scores = result["scores"]
                db.commit()
                return {"status": "ok", "run_id": run_id, "verdict": result["verdict"]}
            kb = db.scalar(_select(KB).where(KB.name == payload["agent"]))
            if kb is None:
                run.verdict = "FAIL"
                run.metrics = {"error": f"知识库 {payload['agent']} 不存在"}
                db.commit()
                return {"status": "failed", "error": run.metrics["error"]}

            case_metrics = []
            scores = []
            for case in dataset.cases:
                try:
                    hits = kb_svc.retrieve(db, kb, case["query"], top_k=top_k)
                    ranked = [h["citation"]["chunk_id"] for h in hits]
                    m = evaluate_retrieval(ranked, case.get("relevant_chunk_ids", []), top_k)
                except Exception as e:
                    m = {"hit_rate": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
                    scores.append({"query": case.get("query", ""), "error": str(e)[:200]})
                    case_metrics.append(m)
                    continue
                case_metrics.append(m)
                scores.append({"query": case["query"], "ranked": ranked[:top_k],
                               "relevant": case.get("relevant_chunk_ids", []), **m})

            metrics = aggregate(case_metrics)
            rate = metrics.get("hit_rate", 0.0)
            run.verdict = "PASS" if rate >= min_rate else "FAIL"
            run.pass_rate = rate
            run.metrics = metrics
            run.scores = scores
            db.commit()
        return {"status": "ok", "run_id": run_id, "metrics": metrics}

    async def _h_agent_invoke(self, payload: dict, prev_result: dict) -> dict:
        """异步执行一次智能体调用（长任务/批量场景）。payload._tenant_id 携带租户时启用策略上下文。"""
        from ..agents.registry import registry
        from ..schemas import InvokeRequest
        from .policy import reset_tenant, set_tenant

        tenant = payload.get("_tenant_id")
        token = set_tenant(int(tenant)) if tenant else None
        from .policy import agent_scope

        agent_token = agent_scope.set(payload["agent"])
        from ..knowledge.acl import acl_from_payload, reset_acl_context, set_acl_context

        acl = acl_from_payload(payload)
        acl_token = set_acl_context(acl) if acl is not None else None
        try:
            with SessionLocal() as db:
                resp = await registry.invoke(db, payload["agent"],
                                             InvokeRequest(input=payload["input"]))
                return {"output": resp.output, "agent_version": resp.agent_version,
                        "citations": [c.model_dump() for c in resp.citations],
                        "steps": resp.steps, "usage": resp.usage}
        finally:
            if token is not None:
                reset_tenant(token)
            if acl_token is not None:
                reset_acl_context(acl_token)
            agent_scope.reset(agent_token)

    async def _h_agent_hitl(self, payload: dict, prev_result: dict) -> dict:
        """带审批门控的智能体执行：审批工具触发 TaskSuspended → WAITING_HUMAN。"""
        from ..agents.registry import registry
        from ..schemas import InvokeRequest
        from .policy import reset_tenant, set_tenant

        tenant = payload.get("_tenant_id")
        tenant_token = set_tenant(int(tenant)) if tenant else None
        from .policy import agent_scope

        agent_token = agent_scope.set(payload["agent"])
        from ..knowledge.acl import acl_from_payload, reset_acl_context, set_acl_context

        acl = acl_from_payload(payload)
        acl_token = set_acl_context(acl) if acl is not None else None
        app = registry.get(payload["agent"]).instance
        if app is None:
            raise RuntimeError(f"智能体 {payload['agent']} 未启动")
        approvals = dict((prev_result or {}).get("approvals", {}))

        def gate(tool_name: str):
            return approvals.get(tool_name)  # True/False/None

        resume = prev_result or None
        try:
            result = await app.on_invoke_task(InvokeRequest(input=payload["input"]),
                                              gate=gate, resume=resume)
        finally:
            if tenant_token is not None:
                reset_tenant(tenant_token)
            if acl_token is not None:
                reset_acl_context(acl_token)
            agent_scope.reset(agent_token)
        return {"output": result.content, "citations": [c.model_dump() for c in result.citations],
                "steps": result.steps, "usage": result.usage}


def create_task_engine(queue_backend=None) -> TaskEngine:
    """每个平台实例独立引擎（Worker 绑定各自事件循环）；队列后端可选 Redis Streams。"""
    engine = TaskEngine(queue_backend)
    engine.register_handler("agent.invoke", engine._h_agent_invoke)
    engine.register_handler("agent.hitl", engine._h_agent_hitl)
    engine.register_handler("kb.ingest", engine._h_kb_ingest)
    engine.register_handler("eval.run", engine._h_eval_run)
    return engine

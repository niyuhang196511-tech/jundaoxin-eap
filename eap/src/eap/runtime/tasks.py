"""Task/Job 引擎：8 态状态机的 M2 子集（docs/03 §5）。

- asyncio 队列 + Worker 池（单实例开发版；多副本换 Redis Streams，接口不变）
- 处理器注册表：按任务类型分发
- HITL：TaskSuspended → WAITING_HUMAN，快照落库，审批后续跑
- 取消：排队中直接标记；运行中经 asyncio 取消传播
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import TaskRecord
from .loop import TaskSuspended

Handler = Callable[[dict, dict], Awaitable[dict]]


@dataclass
class TaskSnapshot:
    """挂起快照：审批续跑所需的全部状态。"""

    messages: list[dict]
    pending_tool: str
    pending_args: str
    approvals: dict


# ---------- 队列后端（docs/03 §5：单实例 asyncio 队列 / 多副本 Redis Streams，接口一致） ----------

class AsyncioQueueBackend:
    """单实例开发版后端：进程内 asyncio.Queue（不持久、不跨实例）。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue | None = None

    async def start(self) -> None:
        self._queue = asyncio.Queue()

    async def stop(self) -> None:
        self._queue = None

    async def enqueue(self, task_id: str) -> None:
        if self._queue is not None:
            self._queue.put_nowait(task_id)

    async def get(self) -> tuple[str, str]:
        """返回 (task_id, receipt)；asyncio 后端 receipt 即 task_id。"""
        task_id = await self._queue.get()
        return task_id, task_id

    async def ack(self, receipt: str) -> None:
        if self._queue is not None:
            self._queue.task_done()


class RedisStreamBackend:
    """多副本后端（docs/03 §5）：Redis Streams + 消费组。

    - 提交 XADD；消费 XREADGROUP（block 轮询）；完成 XACK
    - 启动时 XAUTOCLAIM 接管空闲 >60s 的 pending 条目——前一实例崩溃遗留的任务自动重投
    - 消费者名含随机后缀：每个副本独立身份
    """

    STREAM = "eap:tasks"
    GROUP = "eap-workers"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)
        self._consumer = f"worker-{uuid.uuid4().hex[:8]}"
        self._reclaimer: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            await self._r.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        # 崩溃恢复：接管其他实例遗留的 pending（空闲超过 60 秒）
        await self._reclaim_stale()
        # 运行期周期接管（M8）：某实例处理中崩溃后，存活实例自动接管其 pending 消息
        self._reclaimer = asyncio.create_task(self._reclaim_loop())

    async def _reclaim_stale(self) -> int:
        """XAUTOCLAIM 接管空闲超 60s 的 pending → 重新入队（幂等：XACK 原消息）。"""
        try:
            claimed = await self._r.xautoclaim(self.STREAM, self.GROUP, self._consumer,
                                               min_idle_time=60_000, count=20)
            messages = claimed[1] if isinstance(claimed, (tuple, list)) else []
            recovered = 0
            for entry in messages:
                msg_id, fields = (entry[0], entry[1]) if isinstance(entry, tuple) else (entry, {})
                if fields.get("task_id"):
                    await self._r.xadd(self.STREAM, {"task_id": fields["task_id"]})
                    await self._r.xack(self.STREAM, self.GROUP, msg_id)
                    recovered += 1
            if recovered:
                logging.getLogger("eap.tasks").info("接管 %d 条遗留 pending 消息", recovered)
            return recovered
        except Exception:
            return 0  # 恢复失败不阻塞启动/运行（Redis 兼容实现差异）

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

    async def enqueue(self, task_id: str) -> None:
        await self._r.xadd(self.STREAM, {"task_id": task_id})

    async def get(self) -> tuple[str, str]:
        while True:
            resp = await self._r.xreadgroup(self.GROUP, self._consumer,
                                            {self.STREAM: ">"}, count=1, block=60_000)
            for _stream, messages in resp or []:
                for msg_id, fields in messages:
                    task_id = fields.get("task_id")
                    if task_id:
                        return task_id, msg_id

    async def ack(self, receipt: str) -> None:
        await self._r.xack(self.STREAM, self.GROUP, receipt)


class TaskEngine:
    def __init__(self, queue_backend=None) -> None:
        self.handlers: dict[str, Handler] = {}
        self._backend = queue_backend
        self._workers: list[asyncio.Task] = []
        self._scheduler: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()

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
        await self._recover_pending()

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
        for w in self._workers:
            w.cancel()
        if self._scheduler is not None:
            self._scheduler.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        if self._scheduler is not None:
            await asyncio.gather(self._scheduler, return_exceptions=True)
        self._workers = []
        self._scheduler = None
        if self._backend is not None:
            await self._backend.stop()

    # ---------- 提交 / 查询 ----------

    async def submit(self, db: Session, task_type: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex
        record = TaskRecord(id=task_id, type=task_type, state="PENDING", payload=payload)
        db.add(record)
        db.commit()
        await self.enqueue(task_id)
        return task_id

    async def enqueue(self, task_id: str) -> None:
        await self._backend.enqueue(task_id)

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

    # ---------- 定时调度（docs/03 §5）：到期即 submit，走统一队列/HITL/取消链路 ----------

    async def _schedule_loop(self) -> None:
        from datetime import datetime, timedelta, timezone

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
                            .values(next_run_at=now + timedelta(seconds=s.interval_seconds),
                                    last_run_at=now)
                        ).rowcount
                        if claimed:
                            await self.submit(db, s.task_type, dict(s.payload or {}))
                    db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.getLogger("eap.tasks").warning("调度循环异常: %s", e)
            await asyncio.sleep(1)

    # ---------- 执行 ----------

    async def _worker(self, i: int) -> None:
        while True:
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
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None or task.state not in ("PENDING",):
                return
            task.state = "RUNNING"
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
            return
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                self._mark(task_id, "CANCELLED", {"error": "被人工取消"})
                return
            raise
        except Exception as e:
            self._mark(task_id, "FAILED", {"error": str(e)})
            return
        finally:
            self._running.pop(task_id, None)
            self._cancel_requested.discard(task_id)

        self._mark(task_id, "COMPLETED", result)

    def _mark(self, task_id: str, state: str, result: dict) -> None:
        from ..observability.metrics import incr

        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                return
            task.state = state
            task.result = result
            db.commit()
        try:
            incr("eap_tasks_total", {"state": state})
        except Exception:
            pass

    # ---------- 内置处理器 ----------

    async def _h_kb_ingest(self, payload: dict, prev_result: dict) -> dict:
        """异步文档摄入（M12）：大文档解析+分块+嵌入不阻塞请求线程。"""
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
                                   base64.b64decode(payload["file_b64"]))
        if not content.strip():
            return {"status": "failed", "error": "解析后内容为空"}

        with SessionLocal() as db:
            kb = db.scalar(select(KB).where(KB.name == kb_name))
            if kb is None:
                return {"status": "failed", "error": f"知识库 {kb_name} 不存在"}
            doc = kb_svc.ingest_text(db, kb, title, content,
                                     source=str(payload.get("source", "upload")))
            chunk_count = len(db.scalars(
                select(Chunk.id).where(Chunk.doc_id == doc.id)).all())
            return {"status": "ok", "document_id": doc.id, "title": title,
                    "chunks": chunk_count}

    async def _h_agent_invoke(self, payload: dict, prev_result: dict) -> dict:
        """异步执行一次智能体调用（长任务/批量场景）。"""
        from ..agents.registry import registry
        from ..schemas import InvokeRequest

        with SessionLocal() as db:
            resp = await registry.invoke(db, payload["agent"],
                                         InvokeRequest(input=payload["input"]))
            return {"output": resp.output, "agent_version": resp.agent_version,
                    "citations": [c.model_dump() for c in resp.citations],
                    "steps": resp.steps, "usage": resp.usage}

    async def _h_agent_hitl(self, payload: dict, prev_result: dict) -> dict:
        """带审批门控的智能体执行：审批工具触发 TaskSuspended → WAITING_HUMAN。"""
        from ..agents.registry import registry
        from ..schemas import InvokeRequest

        app = registry.get(payload["agent"]).instance
        if app is None:
            raise RuntimeError(f"智能体 {payload['agent']} 未启动")
        approvals = dict((prev_result or {}).get("approvals", {}))

        def gate(tool_name: str):
            return approvals.get(tool_name)  # True/False/None

        resume = prev_result or None
        result = await app.on_invoke_task(InvokeRequest(input=payload["input"]),
                                          gate=gate, resume=resume)
        return {"output": result.content, "citations": [c.model_dump() for c in result.citations],
                "steps": result.steps, "usage": result.usage}


def create_task_engine(queue_backend=None) -> TaskEngine:
    """每个平台实例独立引擎（Worker 绑定各自事件循环）；队列后端可选 Redis Streams。"""
    engine = TaskEngine(queue_backend)
    engine.register_handler("agent.invoke", engine._h_agent_invoke)
    engine.register_handler("agent.hitl", engine._h_agent_hitl)
    engine.register_handler("kb.ingest", engine._h_kb_ingest)
    return engine

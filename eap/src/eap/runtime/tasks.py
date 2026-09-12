"""Task/Job 引擎：8 态状态机的 M2 子集（docs/03 §5）。

- asyncio 队列 + Worker 池（单实例开发版；多副本换 Redis Streams，接口不变）
- 处理器注册表：按任务类型分发
- HITL：TaskSuspended → WAITING_HUMAN，快照落库，审批后续跑
- 取消：排队中直接标记；运行中经 asyncio 取消传播
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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


class TaskEngine:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()

    def register_handler(self, task_type: str, handler: Handler) -> None:
        self.handlers[task_type] = handler

    # ---------- 生命周期 ----------

    async def start(self, workers: int = 2) -> None:
        self._queue = asyncio.Queue()
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(workers)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._queue = None

    # ---------- 提交 / 查询 ----------

    async def submit(self, db: Session, task_type: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex
        record = TaskRecord(id=task_id, type=task_type, state="PENDING", payload=payload)
        db.add(record)
        db.commit()
        self.enqueue(task_id)
        return task_id

    def enqueue(self, task_id: str) -> None:
        if self._queue is not None:
            self._queue.put_nowait(task_id)

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
        self.enqueue(task_id)
        return "PENDING"

    # ---------- 执行 ----------

    async def _worker(self, i: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 处理器之外的意外错误
                self._mark(task_id, "FAILED", {"error": str(e)})
            finally:
                self._queue.task_done()

    async def _run_one(self, task_id: str) -> None:
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
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task is None:
                return
            task.state = state
            task.result = result
            db.commit()

    # ---------- 内置处理器 ----------

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


def create_task_engine() -> TaskEngine:
    """每个平台实例独立引擎（队列/Worker 绑定各自事件循环，不可跨实例复用）。"""
    engine = TaskEngine()
    engine.register_handler("agent.invoke", engine._h_agent_invoke)
    engine.register_handler("agent.hitl", engine._h_agent_hitl)
    return engine

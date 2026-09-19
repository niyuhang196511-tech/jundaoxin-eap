"""工作流服务：DSL 存储 → 动态注册为智能体 → 平台纳管（docs/03 §6）。"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agents.registry import registry
from .db import SessionLocal
from .models import WorkflowRecord, WorkflowRunRecord
from .runtime.workflow import WorkflowSpec, create_workflow_agent_class, execute_workflow


async def create_and_register(db: Session, spec: WorkflowSpec) -> WorkflowRecord:
    """持久化 DSL → 动态构建 AgentApp → 注册 → 启动（对平台即普通智能体）。"""
    if db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == spec.name)):
        raise ValueError(f"工作流 {spec.name} 已存在")
    record = WorkflowRecord(name=spec.name, version=spec.version, dsl=spec.model_dump())
    db.add(record)
    db.commit()

    cls = create_workflow_agent_class(spec)
    registry.register(cls, cls.manifest, source="workflow", module=f"workflow:{spec.name}")
    await registry.start_agent(spec.name)
    return record


async def load_enabled() -> int:
    """平台启动时：把已启用的 DSL 工作流重新注册为智能体。"""
    count = 0
    with SessionLocal() as db:
        for record in db.scalars(select(WorkflowRecord).where(WorkflowRecord.enabled == True)).all():  # noqa: E712
            try:
                spec = WorkflowSpec(**record.dsl)
                cls = create_workflow_agent_class(spec)
                registry.register(cls, cls.manifest, source="workflow",
                                  module=f"workflow:{spec.name}")
                await registry.start_agent(spec.name)
                count += 1
            except Exception as e:
                logging.getLogger("eap.workflow").warning("%s 注册失败: %s", record.name, e)
    return count


async def disable(name: str) -> None:
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        if record is None:
            raise KeyError(f"工作流 {name} 不存在")
        record.enabled = False
        db.commit()
    registry._agents.pop(name, None)  # 从目录摘除（版本历史仍在 DB）


# ---------- 运行记录（DSL v2：逐节点执行历史，画布试运行可视化数据源） ----------

def _flush_run(run_id: str, *, status: str, output: str, error: str,
               node_runs: list[dict], elapsed_ms: int,
               variables: dict | None = None, pending_node: str | None = None) -> None:
    """运行记录落库（每个节点事件后调用，前端轮询可见中间态）。"""
    with SessionLocal() as db:
        record = db.get(WorkflowRunRecord, run_id)
        if record is None:
            return
        record.status = status
        record.output = output
        record.error = error
        record.node_runs = node_runs
        record.elapsed_ms = elapsed_ms
        if variables is not None:
            record.variables = variables
        if pending_node is not None:
            record.pending_node = pending_node
        db.commit()


async def test_run_async(name: str, input_text: str) -> str:
    """端点用：预生成 run_id → 立即落 running 记录 → create_task 后台执行 → 返回 run_id。"""
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        if record is None:
            raise KeyError(f"工作流 {name} 不存在")
        run_id = f"run-{uuid4().hex[:12]}"
        db.add(WorkflowRunRecord(id=run_id, workflow=name, version=record.version,
                                 input=input_text))
        db.commit()
    asyncio.get_running_loop().create_task(_run_with_id(name, input_text, run_id))
    return run_id


async def _run_with_id(name: str, input_text: str, run_id: str) -> None:
    started = time.perf_counter()
    node_runs: list[dict] = []

    async def on_event(ev: dict) -> None:
        if ev.get("type") == "start":
            node_runs.append({"id": ev["node"], "type": ev.get("node_type", ""),
                              "status": "running", "output": "", "error": "", "elapsed_ms": 0})
        elif ev.get("type") == "end":
            for nr in reversed(node_runs):
                if nr["id"] == ev["node"] and nr["status"] == "running":
                    nr["status"] = ev.get("status", "ok")
                    nr["output"] = ev.get("output", "")
                    nr["error"] = ev.get("error", "")
                    nr["elapsed_ms"] = ev.get("elapsed_ms", 0)
                    break
        _flush_run(run_id, status="running", output="", error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))

    try:
        from .agents.registry import get_platform_context
        from .runtime.workflow import WorkflowSuspended

        spec = get_spec(name)
        ctx = get_platform_context()
        with ctx.db() as db:
            result = await execute_workflow(spec, ctx, db, input_text, on_event=on_event)
        _flush_run(run_id, status="succeeded", output=str(result.get("output", "")), error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))
    except WorkflowSuspended as sus:
        # 交互引擎（v0.5-⑤）：挂起 → waiting_input + 变量快照，submit 端点续跑；
        # 同时落 interactions 表（复用交互引擎的提交/前端渲染链路）
        _flush_run(run_id, status="waiting_input", output=sus.schema.get("title", ""), error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000),
                   variables=sus.variables, pending_node=sus.node_id)
        with SessionLocal() as db:
            from .models import InteractionRecord

            db.add(InteractionRecord(
                id="itx-" + uuid4().hex[:12], agent=name,
                run_id=run_id, schema=sus.schema, values={}, state="waiting",
            ))
            db.commit()
    except Exception as e:
        _flush_run(run_id, status="failed", output="", error=str(e)[:4000],
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))


async def resume_run_async(name: str, run_id: str, values: dict) -> None:
    """交互提交后：恢复变量快照，从挂起节点继续执行（后台任务，同 test_run 机制）。"""
    started = time.perf_counter()
    node_runs: list[dict] = []

    async def on_event(ev: dict) -> None:
        if ev.get("type") == "start":
            node_runs.append({"id": ev["node"], "type": ev.get("node_type", ""),
                              "status": "running", "output": "", "error": "", "elapsed_ms": 0})
        elif ev.get("type") == "end":
            for nr in reversed(node_runs):
                if nr["id"] == ev["node"] and nr["status"] == "running":
                    nr["status"] = ev.get("status", "ok")
                    nr["output"] = ev.get("output", "")
                    nr["error"] = ev.get("error", "")
                    nr["elapsed_ms"] = ev.get("elapsed_ms", 0)
                    break
        _flush_run(run_id, status="running", output="", error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))

    with SessionLocal() as db:
        run = db.get(WorkflowRunRecord, run_id)
        if run is None or run.status != "waiting_input":
            return
        pending_node = run.pending_node
        variables = dict(run.variables or {})
        run.status = "running"
        db.commit()

    try:
        from .agents.registry import get_platform_context
        from .runtime.workflow import WorkflowSuspended, execute_workflow

        spec = get_spec(name)
        ctx = get_platform_context()
        with ctx.db() as db:
            result = await execute_workflow(spec, ctx, db, "", on_event=on_event,
                                            resume_node=pending_node,
                                            resume_variables=variables,
                                            resume_values=values)
        _flush_run(run_id, status="succeeded", output=str(result.get("output", "")), error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))
    except WorkflowSuspended as sus:
        # 连续多个交互节点：再次挂起
        _flush_run(run_id, status="waiting_input", output=sus.schema.get("title", ""), error="",
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000),
                   variables=sus.variables, pending_node=sus.node_id)
        with SessionLocal() as db:
            from .models import InteractionRecord

            db.add(InteractionRecord(
                id="itx-" + uuid4().hex[:12], agent=name,
                run_id=run_id, schema=sus.schema, values={}, state="waiting",
            ))
            db.commit()
    except Exception as e:
        _flush_run(run_id, status="failed", output="", error=str(e)[:4000],
                   node_runs=[dict(nr) for nr in node_runs],
                   elapsed_ms=int((time.perf_counter() - started) * 1000))


def get_spec(name: str) -> WorkflowSpec:
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        if record is None:
            raise KeyError(f"工作流 {name} 不存在")
        return WorkflowSpec(**record.dsl)

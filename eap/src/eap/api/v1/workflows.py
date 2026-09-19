"""Workflow API：DSL 创建 → 动态注册为智能体 / 目录 / 停用 / 试运行（DSL v2 运行记录）。"""

from __future__ import annotations

import asyncio

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import WorkflowRecord, WorkflowRunRecord
from ...runtime.workflow import WorkflowSpec
from ...workflows import (create_and_register, disable, load_enabled, resume_run_async,
                          test_run_async)
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/workflows",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_workflows(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": w.name, "version": w.version, "enabled": w.enabled,
         "steps": len((w.dsl or {}).get("steps", [])),
         "edges": len((w.dsl or {}).get("edges", []))}
        for w in db.scalars(select(WorkflowRecord)).all()
    ]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
async def create_workflow(body: WorkflowSpec, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """DSL 创建即注册：工作流立刻成为可调用、可嵌入的智能体。"""
    try:
        record = await create_and_register(db, body)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 {e}") from e
    audit.record("workflow.create", actor=audit.actor_of(request), target=record.name,
                 detail={"steps": len(body.steps), "edges": len(body.edges)},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "version": record.version, "steps": len(body.steps),
            "invoke": f"/api/v1/agents/{record.name}/invocations"}


@router.get("/{name}/dsl")
def get_workflow_dsl(name: str, db: Session = fastapi.Depends(get_db)):
    """DSL 全文（画布编辑器数据源）。"""
    record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 工作流 {name} 不存在")
    return record.dsl or {}


@router.post("/{name}/test-run")
async def test_run(name: str, body: dict):
    """后台执行一次试运行，立即返回 run_id（逐节点执行历史落 workflow_runs）。"""
    try:
        run_id = await test_run_async(name, str(body.get("input", "")))
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    return {"run_id": run_id}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = fastapi.Depends(get_db)):
    """运行详情（前端 500ms 轮询驱动节点状态环）。"""
    record = db.get(WorkflowRunRecord, run_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 运行 {run_id} 不存在")
    pending = None
    if record.status == "waiting_input":
        # 交互引擎（v0.5-⑤）：挂起运行的交互信息（复用 interactions 表）
        from ...models import InteractionRecord

        itx = db.scalar(select(InteractionRecord)
                        .where(InteractionRecord.run_id == run_id,
                               InteractionRecord.state == "waiting")
                        .order_by(InteractionRecord.created_at.desc()))
        if itx is not None:
            pending = {"interaction_id": itx.id, "ui_schema": itx.schema}
    return {
        "id": record.id, "workflow": record.workflow, "version": record.version,
        "input": record.input, "output": record.output, "status": record.status,
        "error": record.error, "node_runs": record.node_runs or [],
        "elapsed_ms": record.elapsed_ms, "created_at": str(record.created_at),
        "pending_interaction": pending,
    }


class RunSubmit(BaseModel):
    values: dict = Field(min_length=1, description="用户提交的交互表单值")


@router.post("/runs/{run_id}/submit")
async def submit_run(run_id: str, body: RunSubmit, db: Session = fastapi.Depends(get_db)):
    """交互提交（v0.5-⑤）：恢复变量快照，从挂起节点继续执行（后台任务）。"""
    from ...models import InteractionRecord

    record = db.get(WorkflowRunRecord, run_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 运行 {run_id} 不存在")
    if record.status != "waiting_input":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-4006 运行 {run_id} 状态为 {record.status}，不在等待输入")
    itx = db.scalar(select(InteractionRecord)
                    .where(InteractionRecord.run_id == run_id,
                           InteractionRecord.state == "waiting")
                    .order_by(InteractionRecord.created_at.desc()))
    if itx is not None:
        itx.values = body.values
        itx.state = "submitted"
        db.commit()
    asyncio.get_running_loop().create_task(resume_run_async(record.workflow, run_id, body.values))
    return {"run_id": run_id, "status": "running"}


@router.get("/{name}/runs")
def list_runs(name: str, db: Session = fastapi.Depends(get_db)):
    """工作流历史运行列表（新→旧，截断 50 条）。"""
    records = db.scalars(
        select(WorkflowRunRecord).where(WorkflowRunRecord.workflow == name)
        .order_by(WorkflowRunRecord.created_at.desc()).limit(50)
    ).all()
    return [
        {"id": r.id, "status": r.status, "input": r.input, "output": r.output,
         "error": r.error, "elapsed_ms": r.elapsed_ms, "created_at": str(r.created_at)}
        for r in records
    ]


@router.post("/reload", dependencies=[fastapi.Depends(require_admin)])
async def reload_workflows(request: fastapi.Request):
    """从 DB 重新加载启用的 DSL 工作流（幂等注册）。"""
    count = await load_enabled()
    audit.record("workflow.reload", actor=audit.actor_of(request), target="*",
                 detail={"reloaded": count}, trace_id=getattr(request.state, "trace_id", ""))
    return {"reloaded": count}


@router.delete("/{name}", dependencies=[fastapi.Depends(require_admin)])
async def disable_workflow(name: str, request: fastapi.Request):
    try:
        await disable(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    audit.record("workflow.disable", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "enabled": False}

"""Workflow API：DSL 创建 → 动态注册为智能体 / 目录 / 停用 / 试运行（DSL v2 运行记录）。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import WorkflowRecord, WorkflowRunRecord
from ...runtime.workflow import WorkflowSpec
from ...workflows import create_and_register, disable, load_enabled, test_run_async
from ..deps import require_api_key, resolve_tenant

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


@router.post("")
async def create_workflow(body: WorkflowSpec, db: Session = fastapi.Depends(get_db)):
    """DSL 创建即注册：工作流立刻成为可调用、可嵌入的智能体。"""
    try:
        record = await create_and_register(db, body)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 {e}") from e
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
    return {
        "id": record.id, "workflow": record.workflow, "version": record.version,
        "input": record.input, "output": record.output, "status": record.status,
        "error": record.error, "node_runs": record.node_runs or [],
        "elapsed_ms": record.elapsed_ms, "created_at": str(record.created_at),
    }


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


@router.post("/reload")
async def reload_workflows():
    """从 DB 重新加载启用的 DSL 工作流（幂等注册）。"""
    count = await load_enabled()
    return {"reloaded": count}


@router.delete("/{name}")
async def disable_workflow(name: str):
    try:
        await disable(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    return {"name": name, "enabled": False}

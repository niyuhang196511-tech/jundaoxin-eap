"""Workflow API：DSL 创建 → 动态注册为智能体 / 目录 / 停用。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import WorkflowRecord
from ...runtime.workflow import WorkflowSpec
from ...workflows import create_and_register, disable, load_enabled
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/workflows",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_workflows(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": w.name, "version": w.version, "enabled": w.enabled,
         "steps": len((w.dsl or {}).get("steps", []))}
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

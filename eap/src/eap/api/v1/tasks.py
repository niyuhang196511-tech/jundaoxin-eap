"""Task API：提交（长任务/HITL）/ 查询 / 取消 / 审批（docs/03 §5）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import TaskRecord
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/tasks",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


def _engine(request: fastapi.Request):
    return request.app.state.task_engine


class TaskSubmit(BaseModel):
    type: str
    payload: dict = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: bool
    comment: str = ""


def _view(task: TaskRecord) -> dict:
    result = task.result or {}
    pending = result.get("pending")
    return {
        "task_id": task.id, "type": task.type, "state": task.state,
        "payload": task.payload, "result": result,
        "pending_tool": pending.get("tool") if pending else None,
        "created_at": str(task.created_at), "updated_at": str(task.updated_at),
    }


@router.get("")
def list_tasks(state: str | None = None, db: Session = fastapi.Depends(get_db)):
    query = select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(50)
    if state:
        query = query.where(TaskRecord.state == state)
    return [_view(t) for t in db.scalars(query).all()]


@router.post("")
async def submit_task(body: TaskSubmit, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    try:
        task_id = await _engine(request).submit(db, body.type, body.payload)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return {"task_id": task_id, "state": "PENDING"}


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = fastapi.Depends(get_db)):
    task = db.get(TaskRecord, task_id)
    if task is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 任务 {task_id} 不存在")
    return _view(task)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: fastapi.Request):
    try:
        state = await _engine(request).cancel(task_id)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    return {"task_id": task_id, "state": state}


@router.post("/{task_id}/approve")
async def approve_task(task_id: str, body: ApprovalDecision, request: fastapi.Request):
    """HITL 审批：批准/否决挂起的工具调用，任务续跑。"""
    try:
        state = await _engine(request).approve(task_id, body.decision, body.comment)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-4006 {e}") from e
    return {"task_id": task_id, "state": state}

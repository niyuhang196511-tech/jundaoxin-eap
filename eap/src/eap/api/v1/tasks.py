"""Task API：提交（长任务/HITL）/ 查询 / 取消 / 审批（docs/03 §5）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import TaskRecord, TaskScheduleRecord
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


# ---------- 定时调度（docs/03 §5）：到期由引擎自动提交任务 ----------
# 注意：GET /schedules 必须注册在 GET /{task_id} 之前，否则被路径参数吞掉

class ScheduleCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    task_type: str
    payload: dict = Field(default_factory=dict)
    interval_seconds: int = Field(ge=1, le=86400 * 7)
    note: str = Field(default="", max_length=256)


def _sched_view(s: TaskScheduleRecord) -> dict:
    return {"name": s.name, "task_type": s.task_type, "payload": s.payload,
            "interval_seconds": s.interval_seconds, "enabled": s.enabled,
            "last_run_at": str(s.last_run_at) if s.last_run_at else None,
            "next_run_at": str(s.next_run_at) if s.next_run_at else None, "note": s.note}


@router.get("/schedules")
def list_schedules(db: Session = fastapi.Depends(get_db)):
    return [_sched_view(s) for s in db.scalars(select(TaskScheduleRecord)).all()]


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


# ---------- 定时调度：创建 / 启停 / 删除（列表见上） ----------

@router.post("/schedules")
def create_schedule(body: ScheduleCreate, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    from datetime import datetime, timedelta, timezone

    if db.scalar(select(TaskScheduleRecord).where(TaskScheduleRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 调度 {body.name} 已存在")
    if body.task_type not in _engine(request).handlers:
        raise fastapi.HTTPException(
            status_code=404, detail=f"EAP-4004 未知任务类型 {body.task_type}（未注册处理器）")
    now = datetime.now(timezone.utc)
    record = TaskScheduleRecord(
        name=body.name, task_type=body.task_type, payload=body.payload,
        interval_seconds=body.interval_seconds, note=body.note,
        next_run_at=now + timedelta(seconds=body.interval_seconds))
    db.add(record)
    db.commit()
    return _sched_view(record)


@router.patch("/schedules/{name}")
def toggle_schedule(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    from datetime import datetime, timedelta, timezone

    record = db.scalar(select(TaskScheduleRecord).where(TaskScheduleRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 调度 {name} 不存在")
    record.enabled = enabled
    if enabled and record.next_run_at is None:
        record.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=record.interval_seconds)
    db.commit()
    return {"name": name, "enabled": enabled}


@router.delete("/schedules/{name}")
def delete_schedule(name: str, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(TaskScheduleRecord).where(TaskScheduleRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 调度 {name} 不存在")
    db.delete(record)
    db.commit()
    return {"name": name, "status": "deleted"}

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
    priority: int = Field(default=0, ge=0,
                          description="队列优先级（M32）：数值大优先，同级 FIFO")
    # 任务幂等键（M32）：body 字段而非 Idempotency-Key 头——该头已被 M31 网关
    # 幂等中间件占用（HTTP 层同键重放），任务级去重走 body 字段，两层语义互补
    idempotency_key: str | None = Field(default=None, max_length=128,
                                        description="同键在途任务（PENDING/RUNNING/WAITING_*）命中直接返回")


class ApprovalDecision(BaseModel):
    decision: bool
    comment: str = ""


def _view(task: TaskRecord) -> dict:
    result = task.result or {}
    pending = result.get("pending")
    interaction = result.get("pending_interaction")
    return {
        "task_id": task.id, "type": task.type, "state": task.state,
        "payload": task.payload, "result": result,
        "pending_tool": pending.get("tool") if pending else None,
        "pending_interaction": interaction,  # v0.5 交互引擎：{key,title,description,schema}
        "created_at": str(task.created_at), "updated_at": str(task.updated_at),
    }


@router.get("")
def list_tasks(state: str | None = None, limit: int = 50, offset: int = 0,
               db: Session = fastapi.Depends(get_db)):
    """任务列表（created_at 倒序）。分页（M52-B）：limit 钳制 [1,200]、offset 钳制 ≥0，默认 50/0 向后兼容。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    query = select(TaskRecord)
    if state:
        query = query.where(TaskRecord.state == state)
    query = query.order_by(TaskRecord.created_at.desc()).limit(limit).offset(offset)
    return [_view(t) for t in db.scalars(query).all()]


@router.post("")
async def submit_task(body: TaskSubmit, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    # trace 传播（M11）：提交方 trace_id 注入 payload，task.run span 属性关联
    payload = dict(body.payload or {})
    payload.setdefault("_trace_id", getattr(request.state, "trace_id", ""))
    # ACL 上下文（M36/L6）：提交方身份快照进 payload，任务通道执行时重建检索过滤边界
    # （与 M24 _tenant_id 策略上下文同法；embed/api_key 通道 roles 语义见 deps.resolve_tenant）
    payload.setdefault("_acl", {"tenant_id": getattr(request.state, "tenant_id", None),
                                "user_id": getattr(request.state, "user", None),
                                "roles": getattr(request.state, "roles", [])})
    # 任务幂等键（M32）：body 字段透传（HTTP 层的 Idempotency-Key 头由网关幂等中间件处理）
    try:
        submitted = await _engine(request).submit(db, body.type, payload,
                                                  priority=body.priority,
                                                  idempotency_key=body.idempotency_key)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    task_id = str(submitted)
    state = "PENDING"
    if submitted.existing:  # 命中在途任务：响应其真实状态（非固定 PENDING）
        task = db.get(TaskRecord, task_id)
        if task is not None:
            state = task.state
    return {"task_id": task_id, "state": state, "existing": submitted.existing}


# ---------- 定时调度（docs/03 §5）：到期由引擎自动提交任务 ----------
# 注意：GET /schedules 必须注册在 GET /{task_id} 之前，否则被路径参数吞掉

class ScheduleCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    task_type: str
    payload: dict = Field(default_factory=dict)
    interval_seconds: int = Field(ge=1, le=86400 * 7)
    cron: str | None = Field(default=None, max_length=64,
                             description="5 段 cron 表达式（UTC）；配置后优先于 interval_seconds")
    note: str = Field(default="", max_length=256)


def _sched_view(s: TaskScheduleRecord) -> dict:
    return {"name": s.name, "task_type": s.task_type, "payload": s.payload,
            "interval_seconds": s.interval_seconds, "cron": s.cron, "enabled": s.enabled,
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
    """HITL 审批：批准/否决挂起的工具调用，任务续跑（治理敏感操作 → 审计）。"""
    from ...observability import audit as _audit

    try:
        state = await _engine(request).approve(task_id, body.decision, body.comment)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-4006 {e}") from e
    _audit.record("task.approve", actor=_audit.actor_of(request), target=task_id,
                  detail={"decision": body.decision, "comment": body.comment},
                  trace_id=getattr(request.state, "trace_id", ""))
    return {"task_id": task_id, "state": state}


class InteractionSubmit(BaseModel):
    values: dict = Field(min_length=1, description="用户提交的表单值 {field_id: value}")


@router.post("/{task_id}/interact")
async def interact_task(task_id: str, body: InteractionSubmit, request: fastapi.Request):
    """交互引擎（v0.5-④）：提交 WAITING_INPUT 任务的表单值，任务续跑。"""
    try:
        state = await _engine(request).interact(task_id, body.values)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-4006 {e}") from e
    from ...observability import audit as _audit

    _audit.record("task.interact", actor=_audit.actor_of(request), target=task_id,
                  detail={"fields": sorted(body.values)},
                  trace_id=getattr(request.state, "trace_id", ""))
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
    if body.cron:
        from croniter import croniter

        try:
            croniter(body.cron, now)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 cron 无效: {e}") from e
    first_next = None
    if body.cron:
        first_next = croniter(body.cron, now).get_next(datetime).replace(tzinfo=None)
    record = TaskScheduleRecord(
        name=body.name, task_type=body.task_type, payload=body.payload,
        interval_seconds=body.interval_seconds, cron=body.cron, note=body.note,
        next_run_at=first_next or now + timedelta(seconds=body.interval_seconds))
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

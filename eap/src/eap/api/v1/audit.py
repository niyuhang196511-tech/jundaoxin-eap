"""审计日志查询（M11 / v0.6 增强）：管理面操作追踪（过滤 + 分页）。

过滤维度：action（精确）/ actor（精确）/ target（前缀匹配）/ 时间范围；
分页：limit + offset（新→旧）。
"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import AuditLog
from ..deps import require_admin, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/audit",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_admin)])


@router.get("")
def list_audit(
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = fastapi.Depends(get_db),
):
    """审计日志查询：action 精确 / actor 精确 / target 前缀 / 时间范围（ISO 日期或日期时间）。"""
    from datetime import datetime, timedelta, timezone

    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(AuditLog.actor == actor)
    if target:
        query = query.where(AuditLog.target.startswith(target))
    for field, raw, direction in (("since", since, 1), ("until", until, -1)):
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400,
                                        detail=f"EAP-4000 {field} 须为 ISO 日期/日期时间: {raw}") from e
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if direction == -1:  # until 含当天/当秒：+1 天（仅日期时）由调用方语义决定，这里精确到给定时刻
            query = query.where(AuditLog.created_at <= moment.replace(tzinfo=None))
        else:
            query = query.where(AuditLog.created_at >= moment.replace(tzinfo=None))
    query = (query.order_by(AuditLog.created_at.desc())
             .limit(max(1, min(limit, 500)))
             .offset(max(0, offset)))
    return [
        {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target,
         "detail": a.detail, "trace_id": a.trace_id, "created_at": str(a.created_at)}
        for a in db.scalars(query).all()
    ]

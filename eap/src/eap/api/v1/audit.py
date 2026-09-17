"""审计日志查询（M11）：管理面操作追踪（新→旧，分页）。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import AuditLog
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/audit",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_audit(action: str | None = None, limit: int = 100, db: Session = fastapi.Depends(get_db)):
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
    if action:
        query = query.where(AuditLog.action == action)
    return [
        {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target,
         "detail": a.detail, "trace_id": a.trace_id, "created_at": str(a.created_at)}
        for a in db.scalars(query).all()
    ]

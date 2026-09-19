"""记忆 API：长期记忆写入 / 列表 / 召回 / 遗忘 / 治理（docs/03 §4 + v0.6-⑦）。

租户过滤：JWT 通道按租户过滤与归属；API Key 通道 = 平台管理员语义（可跨租户管理）。
数据权利：按用户批量遗忘与导出（admin）。
"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...observability import audit
from ...runtime.memory import memory_service
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/memory",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class MemoryWrite(BaseModel):
    scope: str = "user"  # user | session
    content: str = Field(min_length=1)
    kind: str = "fact"  # fact | preference | summary
    user_id: str | None = None
    session_id: str | None = None
    agent: str = ""


class MemoryRecall(BaseModel):
    query: str = Field(min_length=1)
    user_id: str | None = None
    session_id: str | None = None
    top_k: int = Field(default=3, ge=1, le=10)


def _tenant_of(request: fastapi.Request) -> int | None:
    """JWT 通道 → 该租户；API Key 通道 → None（平台语义，不过滤）。"""
    if getattr(request.state, "auth_kind", "") == "jwt":
        return getattr(request.state, "tenant_id", None)
    return None


@router.post("")
def remember(body: MemoryWrite, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if body.scope == "user" and not body.user_id:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 用户级记忆需要 user_id")
    if body.scope == "session" and not body.session_id:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 会话级记忆需要 session_id")
    record = memory_service.remember(
        db, scope=body.scope, content=body.content, kind=body.kind,
        user_id=body.user_id, session_id=body.session_id, agent=body.agent,
        tenant_id=_tenant_of(request),
    )
    db.commit()
    return {"id": record.id, "scope": record.scope, "kind": record.kind}


@router.get("")
def list_memories(user_id: str | None = None, session_id: str | None = None,
                  scope: str | None = None, request: fastapi.Request = None,
                  db: Session = fastapi.Depends(get_db)):
    return memory_service.list(db, user_id=user_id, session_id=session_id, scope=scope,
                               tenant_id=_tenant_of(request))


@router.post("/recall")
def recall(body: MemoryRecall, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    return {"hits": memory_service.recall(
        db, body.query, user_id=body.user_id, session_id=body.session_id, top_k=body.top_k,
        tenant_id=_tenant_of(request))}


@router.delete("/{memory_id}")
def forget(memory_id: int, db: Session = fastapi.Depends(get_db)):
    if not memory_service.forget(db, memory_id):
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 记忆 {memory_id} 不存在")
    return {"forgotten": memory_id}


@router.delete("/users/{user_id}", dependencies=[fastapi.Depends(require_admin)])
def forget_user(user_id: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """按用户批量遗忘（v0.6-⑦ 数据权利），落审计。"""
    count = memory_service.forget_user(db, user_id, tenant_id=_tenant_of(request))
    audit.record("memory.forget_user", actor=audit.actor_of(request), target=user_id,
                 detail={"deleted": count}, trace_id=getattr(request.state, "trace_id", ""))
    return {"user_id": user_id, "deleted": count}


@router.get("/users/{user_id}/export")
def export_user(user_id: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """按用户导出全部记忆（v0.6-⑦ 数据权利：可携带）。"""
    return {"user_id": user_id,
            "memories": memory_service.export_user(db, user_id, tenant_id=_tenant_of(request))}


@router.post("/purge", dependencies=[fastapi.Depends(require_admin)])
def purge_expired(request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """保留期清理：删除超过 EAP_MEMORY_RETENTION_DAYS（默认 180）的记忆。"""
    retention = get_settings().memory_retention_days
    count = memory_service.purge_expired(db, retention)
    audit.record("memory.purge", actor=audit.actor_of(request), target="*",
                 detail={"retention_days": retention, "deleted": count},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"deleted": count, "retention_days": retention}

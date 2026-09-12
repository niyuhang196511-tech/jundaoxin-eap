"""记忆 API：长期记忆写入 / 列表 / 召回 / 遗忘（docs/03 §4）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db import get_db
from ...runtime.memory import memory_service
from ..deps import require_api_key, resolve_tenant

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


@router.post("")
def remember(body: MemoryWrite, db: Session = fastapi.Depends(get_db)):
    if body.scope == "user" and not body.user_id:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 用户级记忆需要 user_id")
    if body.scope == "session" and not body.session_id:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 会话级记忆需要 session_id")
    record = memory_service.remember(
        db, scope=body.scope, content=body.content, kind=body.kind,
        user_id=body.user_id, session_id=body.session_id, agent=body.agent,
    )
    db.commit()
    return {"id": record.id, "scope": record.scope, "kind": record.kind}


@router.get("")
def list_memories(user_id: str | None = None, session_id: str | None = None,
                  scope: str | None = None, db: Session = fastapi.Depends(get_db)):
    return memory_service.list(db, user_id=user_id, session_id=session_id, scope=scope)


@router.post("/recall")
def recall(body: MemoryRecall, db: Session = fastapi.Depends(get_db)):
    return {"hits": memory_service.recall(
        db, body.query, user_id=body.user_id, session_id=body.session_id, top_k=body.top_k)}


@router.delete("/{memory_id}")
def forget(memory_id: int, db: Session = fastapi.Depends(get_db)):
    if not memory_service.forget(db, memory_id):
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 记忆 {memory_id} 不存在")
    return {"forgotten": memory_id}

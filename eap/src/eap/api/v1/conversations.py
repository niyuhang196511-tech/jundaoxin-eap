"""会话端点：控制台对话调试的历史会话列表 / 消息回放 / 删除。

数据源为 MemoryRecord（kind=message，runtime/memory.log_message 已在 agent 调用链落库），
不新增表。
"""

from __future__ import annotations

import fastapi
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import MemoryRecord
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/conversations",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_conversations(agent: str | None = None, db: Session = fastapi.Depends(get_db)):
    """会话列表（按最近活跃倒序）：session_id / agent / 消息数 / 最后一条消息。"""
    q = (
        select(
            MemoryRecord.session_id,
            MemoryRecord.agent,
            func.count().label("msg_count"),
            func.max(MemoryRecord.created_at).label("updated_at"),
        )
        .where(MemoryRecord.kind == "message", MemoryRecord.session_id.isnot(None))
        .group_by(MemoryRecord.session_id, MemoryRecord.agent)
    )
    if agent:
        q = q.where(MemoryRecord.agent == agent)
    rows = db.execute(q.order_by(func.max(MemoryRecord.created_at).desc()).limit(100)).all()

    result = []
    for session_id, agent_name, msg_count, updated_at in rows:
        last = db.scalar(
            select(MemoryRecord.content).where(
                MemoryRecord.session_id == session_id, MemoryRecord.kind == "message")
            .order_by(MemoryRecord.id.desc()).limit(1)
        ) or ""
        result.append({
            "session_id": session_id, "agent": agent_name, "messages": msg_count,
            "last_message": last[:80], "updated_at": str(updated_at),
        })
    return result


@router.get("/{session_id}/messages")
def session_messages(session_id: str, db: Session = fastapi.Depends(get_db)):
    """会话消息回放（正序）。meta 携带角色（log_message 写入）。"""
    records = db.scalars(
        select(MemoryRecord)
        .where(MemoryRecord.session_id == session_id, MemoryRecord.kind == "message")
        .order_by(MemoryRecord.id.asc()).limit(200)
    ).all()
    return [
        {"id": r.id, "role": (r.meta or {}).get("role", "user"), "content": r.content,
         "agent": r.agent, "created_at": str(r.created_at)}
        for r in records
    ]


@router.delete("/{session_id}")
def delete_session(session_id: str, db: Session = fastapi.Depends(get_db)):
    deleted = db.query(MemoryRecord).filter(
        MemoryRecord.session_id == session_id, MemoryRecord.kind == "message").delete()
    db.commit()
    return {"session_id": session_id, "deleted": deleted}

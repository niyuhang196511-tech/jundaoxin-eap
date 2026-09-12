"""Memory Service（docs/03 §4）：会话历史 / 长期记忆 / 摘要，可检索（向量+词面）、可遗忘。

- 会话记忆：按 session_id 存对话消息（滑窗由调用方控制）
- 长期记忆：用户事实/偏好，召回时按余弦相似度 + 词面重合打分
- 组织记忆：走知识中心（特殊 KB），此处不重复
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..knowledge.embedding import get_embedder
from ..knowledge.tokenize import tokenize
from ..models import MemoryRecord

MESSAGE = "message"


class MemoryService:
    def _embedder(self):
        return get_embedder(get_settings().embedding_provider, get_settings())

    # ---------- 写入 ----------

    def remember(
        self, db: Session, *, scope: str, content: str, kind: str = "fact",
        session_id: str | None = None, user_id: str | None = None,
        agent: str = "", meta: dict | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            scope=scope, kind=kind, content=content,
            session_id=session_id, user_id=user_id, agent=agent,
            embedding=self._embedder().embed(content),
            meta=meta or {},
        )
        db.add(record)
        db.flush()
        return record

    def log_message(self, db: Session, *, session_id: str, role: str,
                    content: str, agent: str = "") -> None:
        """会话记忆：逐条落库（role: user/assistant）。"""
        self.remember(db, scope="session", kind=MESSAGE, content=content,
                      session_id=session_id, agent=agent, meta={"role": role})

    # ---------- 读取 ----------

    def history(self, db: Session, session_id: str, limit: int = 6) -> list[dict]:
        """会话消息（时间正序，取最近 limit 条）。按自增 id 排序：同一运行内时间戳可能相同。"""
        rows = db.scalars(
            select(MemoryRecord)
            .where(MemoryRecord.session_id == session_id, MemoryRecord.kind == MESSAGE)
            .order_by(MemoryRecord.id.desc())
            .limit(limit)
        ).all()
        return [{"role": r.meta.get("role", "user"), "content": r.content}
                for r in reversed(rows)]

    def recall(self, db: Session, query: str, *, user_id: str | None = None,
               session_id: str | None = None, top_k: int = 3) -> list[dict]:
        """长期记忆召回：余弦 + 词面重合混合打分（不含原始消息，避免噪声）。"""
        embedder = self._embedder()
        q = select(MemoryRecord).where(MemoryRecord.kind != MESSAGE)
        if user_id:
            q = q.where(MemoryRecord.user_id == user_id)
        if session_id:
            q = q.where(MemoryRecord.session_id == session_id)
        rows = db.scalars(q).all()
        if not rows:
            return []

        q_tokens = set(tokenize(query))
        q_vec = embedder.embed(query)
        scored = []
        for r in rows:
            cos = _cosine(q_vec, r.embedding or [])
            overlap = len(q_tokens & set(tokenize(r.content))) / (len(q_tokens) or 1)
            scored.append((cos + 0.3 * overlap, r))
        scored.sort(key=lambda t: -t[0])
        return [{"id": r.id, "kind": r.kind, "content": r.content,
                 "score": round(s, 4), "created_at": str(r.created_at)}
                for s, r in scored[:top_k] if s > 0]

    def list(self, db: Session, *, user_id: str | None = None,
             session_id: str | None = None, scope: str | None = None) -> list[dict]:
        q = select(MemoryRecord).order_by(MemoryRecord.created_at.desc()).limit(100)
        if scope:
            q = q.where(MemoryRecord.scope == scope)
        if user_id:
            q = q.where(MemoryRecord.user_id == user_id)
        if session_id:
            q = q.where(MemoryRecord.session_id == session_id)
        return [
            {"id": r.id, "scope": r.scope, "kind": r.kind, "content": r.content,
             "user_id": r.user_id, "session_id": r.session_id, "created_at": str(r.created_at)}
            for r in db.scalars(q).all()
        ]

    # ---------- 遗忘 ----------

    def forget(self, db: Session, memory_id: int) -> bool:
        record = db.get(MemoryRecord, memory_id)
        if record is None:
            return False
        db.delete(record)
        db.commit()
        return True


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


memory_service = MemoryService()

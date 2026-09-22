"""Memory Service（docs/03 §4）：会话历史 / 长期记忆 / 摘要，可检索（向量+词面）、可遗忘。

- 会话记忆：按 session_id 存对话消息（滑窗由调用方控制）
- 长期记忆：用户事实/偏好，召回时按余弦相似度 + 词面重合 + 重要性加权打分
- scope 分层（M34/L7）：session（会话）| user（用户）| agent（智能体长期记忆，按 agent 列
  过滤）| org（租户内组织共享，session_id/user_id 为空）；不传 scope 时保持 v0.9.0 的
  session/user 原语义（向后兼容）
- 组织记忆：走知识中心（特殊 KB，M41-A consolidation_candidates 供沉淀端点挑选），此处不重复
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..knowledge.embedding import get_embedder
from ..knowledge.tokenize import tokenize
from ..models import MemoryRecord

MESSAGE = "message"

# scope 值域（M34/L7 分层扩展）
SCOPES = ("session", "user", "agent", "org")

# 召回打分权重（M34/L7）：final = cos + W_OVERLAP*overlap + W_IMPORTANCE*importance。
# 各分量归一说明：cos 为查询向量与记忆向量的余弦相似度 ∈ [-1,1]（实际召回中相关内容
# 通常落在 (0,1]）；overlap 为查询词元集合与记忆内容词元集合的重合率 ∈ [0,1]
# （|交集|/|查询词元|）；importance 为写入时给定的重要性权重 ∈ [0,1]（API 校验）。
# 召回门槛沿用 v0.9.0 语义：相关性部分（cos + 0.3*overlap）> 0 才返回——importance
# 只影响排序（高重要性记忆在同等相关度下排前），不会把完全不相关的记忆捞进来。
W_OVERLAP = 0.3
W_IMPORTANCE = 0.2


def utcnow() -> datetime:
    """库内时间统一为无时区 UTC（与 models.utcnow 落库口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_scopes(scope: str | None) -> list[str]:
    """scope 参数解析：支持逗号组合（如 "session,user"）；未知名直接报错。"""
    if not scope:
        return []
    scopes = [s.strip() for s in scope.split(",") if s.strip()]
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ValueError(f"未知 scope: {unknown}（合法值 {'|'.join(SCOPES)}）")
    return scopes


class MemoryService:
    def _embedder(self):
        return get_embedder(get_settings().embedding_provider, get_settings())

    # ---------- 写入 ----------

    def remember(
        self, db: Session, *, scope: str, content: str, kind: str = "fact",
        session_id: str | None = None, user_id: str | None = None,
        agent: str = "", meta: dict | None = None, tenant_id: int | None = None,
        importance: float = 0.5, ttl_days: int | None = None,
    ) -> MemoryRecord:
        """写入一条记忆（M34/L7：importance 权重 + TTL）。

        - importance ∈ [0,1]（越界抛 ValueError）
        - ttl_days > 0 时转 expires_at = now + ttl_days 天；不传为永不过期
        - scope=agent 需带 agent（智能体自己的长期记忆）；scope=org 为租户内共享，
          session_id/user_id 强制置空
        """
        if scope not in SCOPES:
            raise ValueError(f"未知 scope: {scope}（合法值 {'|'.join(SCOPES)}）")
        if not 0.0 <= float(importance) <= 1.0:
            raise ValueError("importance 必须在 0~1 之间")
        if scope == "agent" and not agent:
            raise ValueError("agent 级记忆需要 agent 参数")
        if scope == "org":
            session_id, user_id = None, None
        # 租户归属（v0.6-⑦）：调用方（policy 上下文）未显式给定时取当前租户作用域
        if tenant_id is None:
            from .policy import tenant_scope

            tenant_id = tenant_scope.get()
        expires_at = utcnow() + timedelta(days=ttl_days) if ttl_days and ttl_days > 0 else None
        record = MemoryRecord(
            scope=scope, kind=kind, content=content,
            session_id=session_id, user_id=user_id, agent=agent,
            tenant_id=tenant_id or 1,
            embedding=self._embedder().embed(content),
            meta=meta or {},
            importance=float(importance),
            expires_at=expires_at,
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

    def history(self, db: Session, session_id: str, limit: int = 6,
                tenant_id: int | None = None) -> list[dict]:
        """会话消息（时间正序，取最近 limit 条）。按自增 id 排序：同一运行内时间戳可能相同。"""
        q = (select(MemoryRecord)
             .where(MemoryRecord.session_id == session_id, MemoryRecord.kind == MESSAGE))
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        rows = db.scalars(q.order_by(MemoryRecord.id.desc()).limit(limit)).all()
        return [{"role": r.meta.get("role", "user"), "content": r.content}
                for r in reversed(rows)]

    @staticmethod
    def _alive(q, *, include_expired: bool = False):
        """TTL 过滤（M34/L7）：recall/list 默认排除 expires_at 已过期的记忆。"""
        if include_expired:
            return q
        now = utcnow()
        return q.where(or_(MemoryRecord.expires_at.is_(None),
                           MemoryRecord.expires_at > now))

    def recall(self, db: Session, query: str, *, user_id: str | None = None,
               session_id: str | None = None, top_k: int = 3,
               tenant_id: int | None = None, scope: str | None = None,
               agent: str | None = None, include_expired: bool = False) -> list[dict]:
        """长期记忆召回：余弦 + 词面重合 + 重要性混合打分（不含原始消息，避免噪声）。

        - 不传 scope：v0.9.0 原语义（按 user_id/session_id 过滤）
        - scope=agent：按 agent 列过滤（必带 agent 参数，否则 ValueError）
        - scope=org：租户内共享记忆（session_id/user_id 为空的行）
        - scope 支持逗号组合（"session,user"），并可与 user_id/session_id/agent 组合过滤
        - 默认排除已过期（expires_at < now）；include_expired=True 时不过滤
        """
        scopes = _parse_scopes(scope)
        if "agent" in scopes and not agent:
            raise ValueError("scope=agent 时必须提供 agent 参数")
        embedder = self._embedder()
        q = select(MemoryRecord).where(MemoryRecord.kind != MESSAGE)
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        if scopes:
            q = q.where(MemoryRecord.scope.in_(scopes))
        if agent:
            q = q.where(MemoryRecord.agent == agent)
        if user_id:
            q = q.where(MemoryRecord.user_id == user_id)
        if session_id:
            q = q.where(MemoryRecord.session_id == session_id)
        q = self._alive(q, include_expired=include_expired)
        rows = db.scalars(q).all()
        if not rows:
            return []

        q_tokens = set(tokenize(query))
        q_vec = embedder.embed(query)
        scored = []
        for r in rows:
            cos = _cosine(q_vec, r.embedding or [])
            overlap = len(q_tokens & set(tokenize(r.content))) / (len(q_tokens) or 1)
            relevance = cos + W_OVERLAP * overlap
            final = relevance + W_IMPORTANCE * float(r.importance or 0.0)
            scored.append((relevance, final, r))
        # 相关性门槛（与 v0.9.0 一致），排序按 final（含 importance 加权）
        scored = [t for t in scored if t[0] > 0]
        scored.sort(key=lambda t: -t[1])
        return [{"id": r.id, "kind": r.kind, "scope": r.scope, "content": r.content,
                 "score": round(final, 4), "importance": r.importance,
                 "created_at": str(r.created_at)}
                for relevance, final, r in scored[:top_k]]

    def list(self, db: Session, *, user_id: str | None = None,
             session_id: str | None = None, scope: str | None = None,
             tenant_id: int | None = None, agent: str | None = None,
             importance_min: float | None = None, include_expired: bool = False) -> list[dict]:
        """记忆列表（M34/L7：支持 scope 组合 / agent 过滤 / importance 过滤 / TTL 过滤）。"""
        scopes = _parse_scopes(scope)
        if "agent" in scopes and not agent:
            raise ValueError("scope=agent 时必须提供 agent 参数")
        q = select(MemoryRecord).order_by(MemoryRecord.created_at.desc()).limit(100)
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        if scopes:
            q = q.where(MemoryRecord.scope.in_(scopes))
        if agent:
            q = q.where(MemoryRecord.agent == agent)
        if user_id:
            q = q.where(MemoryRecord.user_id == user_id)
        if session_id:
            q = q.where(MemoryRecord.session_id == session_id)
        if importance_min is not None:
            q = q.where(MemoryRecord.importance >= importance_min)
        q = self._alive(q, include_expired=include_expired)
        return [
            {"id": r.id, "scope": r.scope, "kind": r.kind, "content": r.content,
             "user_id": r.user_id, "session_id": r.session_id, "agent": r.agent,
             "importance": r.importance, "expires_at": str(r.expires_at) if r.expires_at else None,
             "created_at": str(r.created_at)}
            for r in db.scalars(q).all()
        ]

    # ---------- 组织记忆沉淀（M41-A，docs/18 §五） ----------

    def consolidation_candidates(self, db: Session, *, tenant_id: int | None = None,
                                 min_importance: float = 0.7,
                                 limit: int = 50) -> list[MemoryRecord]:
        """组织记忆沉淀候选：scope=org 且 importance ≥ 阈值且 meta 未标记 consolidated、
        未过期（TTL 口径与 recall/list 一致）的记忆；按 created_at 升序取 limit 条
        （先入先沉淀，分批稳定）。"""
        q = select(MemoryRecord).where(MemoryRecord.scope == "org",
                                       MemoryRecord.importance >= min_importance)
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        q = self._alive(q)
        rows = [r for r in db.scalars(q).all() if not (r.meta or {}).get("consolidated")]
        rows.sort(key=lambda r: (r.created_at or utcnow(), r.id))
        return rows[:max(1, limit)]

    # ---------- 遗忘 ----------

    def forget(self, db: Session, memory_id: int) -> bool:
        record = db.get(MemoryRecord, memory_id)
        if record is None:
            return False
        db.delete(record)
        db.commit()
        return True

    def forget_user(self, db: Session, user_id: str, tenant_id: int | None = None) -> int:
        """按用户批量遗忘（v0.6-⑦ 数据权利：删除该用户全部记忆）。"""
        q = select(MemoryRecord).where(MemoryRecord.user_id == user_id)
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        rows = db.scalars(q).all()
        for r in rows:
            db.delete(r)
        db.commit()
        return len(rows)

    def export_user(self, db: Session, user_id: str, tenant_id: int | None = None) -> list[dict]:
        """按用户导出全部记忆（v0.6-⑦ 数据权利：可携带）。"""
        q = select(MemoryRecord).where(MemoryRecord.user_id == user_id)
        if tenant_id is not None:
            q = q.where(MemoryRecord.tenant_id == tenant_id)
        rows = db.scalars(q.order_by(MemoryRecord.created_at)).all()
        return [{"id": r.id, "scope": r.scope, "kind": r.kind, "content": r.content,
                 "agent": r.agent, "session_id": r.session_id,
                 "created_at": str(r.created_at)} for r in rows]

    def purge_expired(self, db: Session, retention_days: int) -> int:
        """保留期清理（v0.6-⑦ + M34/L7）：删除 created_at 早于保留期的记忆，
        以及 expires_at 已过期（TTL 到期）的记忆，返回清理条数。"""
        cutoff = utcnow() - timedelta(days=retention_days)
        now = utcnow()
        rows = db.scalars(select(MemoryRecord).where(
            or_(MemoryRecord.created_at < cutoff,
                MemoryRecord.expires_at < now))).all()
        for r in rows:
            db.delete(r)
        db.commit()
        return len(rows)


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


memory_service = MemoryService()

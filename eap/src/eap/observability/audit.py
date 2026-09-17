"""审计日志（M11）：管理面写操作统一落库。

record(action, target, detail=...) 在路由写端点显式调用（actor 取请求凭证身份）；
敏感字段（api_key/secret/password）递归脱敏。查询端点在 api/v1/audit.py。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AuditLog

_SENSITIVE_KEYS = {"api_key", "secret", "password", "token", "webhook_url", "key"}


def _sanitize(value, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else _sanitize(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, depth + 1) for v in value]
    return value


def record(action: str, *, actor: str = "", target: str = "", detail: dict | None = None,
           trace_id: str = "", db: Session | None = None) -> None:
    """审计落库（失败仅告警，不阻断业务）。db 传入则复用请求级事务。"""
    try:
        entry = AuditLog(
            actor=actor or "system", action=action, target=target,
            detail=_sanitize(detail or {}), trace_id=trace_id,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        if db is not None:
            db.add(entry)
            db.flush()  # 随业务同一事务提交
        else:
            with SessionLocal() as own_db:
                own_db.add(entry)
                own_db.commit()
    except Exception as e:
        logging.getLogger("eap.audit").warning("审计落库失败: %s", e)


def actor_of(request) -> str:
    """从请求态提取操作者身份（resolve_tenant 已填充）。"""
    kind = getattr(request.state, "auth_kind", "")
    if kind == "jwt":
        return f"jwt:{getattr(request.state, 'user', '')}"
    if kind == "api_key":
        return "api-key"
    if kind == "embed_session":
        return "embed"
    return "anonymous"

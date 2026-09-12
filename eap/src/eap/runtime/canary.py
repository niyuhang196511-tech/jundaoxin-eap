"""Canary 灰度路由（docs/06 §2，M3）：按稳定 hash 把部分流量导到 canary 发布。

分流键：user_id → session_id → trace_id（有会话键则同一用户稳定命中同一版本，
否则按请求随机近似比例）。命中后经 contextvar 把 overrides.model 注入模型网关
的 prefer（显式 prefer 优先于灰度覆盖）。委派的子智能体共享同一覆盖。
"""

from __future__ import annotations

import contextvars
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentReleaseRecord

canary_model: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "eap_canary_model", default=None)


def current_model_override() -> str | None:
    return canary_model.get()


def pick_canary(db: Session, agent: str, key: str) -> AgentReleaseRecord | None:
    """该调用是否命中 canary：稳定 hash(agent:key) % 100 < canary_percent。"""
    release = db.scalar(
        select(AgentReleaseRecord)
        .where(AgentReleaseRecord.agent == agent,
               AgentReleaseRecord.state == "canary",
               AgentReleaseRecord.canary_percent > 0)
        .order_by(AgentReleaseRecord.updated_at.desc())
    )
    if release is None:
        return None
    digest = hashlib.sha256(f"{agent}:{key}".encode()).hexdigest()
    return release if int(digest[:8], 16) % 100 < release.canary_percent else None


def apply_override(model: str | None) -> contextvars.Token:
    return canary_model.set(model)


def reset_override(token: contextvars.Token) -> None:
    canary_model.reset(token)

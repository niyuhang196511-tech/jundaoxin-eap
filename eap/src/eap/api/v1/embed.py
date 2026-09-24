"""嵌入外链 API：渠道管理 + 会话换取（docs/04 §6、07 §6）。"""

from __future__ import annotations

import secrets

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...api.security import PREFIX_EMBED, domain_allowed, rate_limiter, sign_session
from ...db import get_db
from ...observability import audit
from ...models import EmbedChannel
from ..deps import resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1", dependencies=[fastapi.Depends(resolve_tenant)])

# 会话换取是公开端点：凭 EmbedToken 自校验（渠道有效性+域名白名单+频控），不走 API Key 依赖
public_router = fastapi.APIRouter(prefix="/api/v1/embed")


class EmbedCreate(BaseModel):
    domains: list[str] = Field(default_factory=lambda: ["*"],
                               description="域名白名单，[\"*\"] 表示不限（开发用）")
    note: str = ""


class EmbedSessionRequest(BaseModel):
    user_id: str = ""


@router.post("/agents/{name}/embed")
def create_embed_channel(name: str, body: EmbedCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """为智能体创建嵌入渠道 → 返回 EmbedToken（第三方页面据此换取会话）。"""
    token = PREFIX_EMBED + secrets.token_urlsafe(24)
    channel = EmbedChannel(agent_name=name, token=token, domains=body.domains, note=body.note)
    db.add(channel)
    db.commit()
    audit.record("embed.create", actor=audit.actor_of(request), target=f"{name}/{channel.id}",
                 trace_id=getattr(request.state, "trace_id", ""))
    return {
        "channel_id": channel.id, "agent": name, "token": token,
        "domains": channel.domains, "status": channel.status,
        "usage": f'<eap-chat agent="{name}" endpoint="https://your-eap-host" token="{token}"></eap-chat>',
    }


@router.get("/agents/{name}/embed")
def list_embed_channels(name: str, db: Session = fastapi.Depends(get_db)):
    return [
        {"channel_id": c.id, "domains": c.domains, "status": c.status, "note": c.note,
         "token_preview": c.token[:12] + "…"}
        for c in db.scalars(select(EmbedChannel).where(EmbedChannel.agent_name == name)).all()
    ]


@router.delete("/embed/{channel_id}")
def disable_embed_channel(channel_id: int, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """停用渠道：EmbedToken 换取即拒，且该渠道已签发的会话令牌立即失效（M51-D 渠道级吊销——
    verify_session 按 payload.cid 回查渠道状态，无需等令牌自然过期）。审计 embed.disable。"""
    channel = db.get(EmbedChannel, channel_id)
    if channel is None:
        raise fastapi.HTTPException(status_code=404, detail="EAP-4004 渠道不存在")
    channel.status = "disabled"
    db.commit()
    audit.record("embed.disable", actor=audit.actor_of(request), target=str(channel_id),
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"channel_id": channel_id, "status": "disabled"}


@public_router.post("/session")
async def exchange_session(
    body: EmbedSessionRequest,
    request: fastapi.Request,
    db: Session = fastapi.Depends(get_db),
):
    """EmbedToken → 短时会话令牌。

    校验链：渠道启用 → 域名白名单（Origin/Referer）→ 频控 → 签发（绑定 agent+租户+渠道+过期；
    渠道绑定 cid 是 M51-D 渠道级吊销依据——渠道 disable 后其存量会话令牌立即失效）。
    """
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        raise fastapi.HTTPException(status_code=401, detail="EAP-1001 缺失 EmbedToken")

    channel = db.scalar(select(EmbedChannel).where(EmbedChannel.token == token))
    if channel is None or channel.status != "enabled":
        raise fastapi.HTTPException(status_code=401, detail="EAP-1003 EmbedToken 无效或已停用")

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if not domain_allowed(channel.domains, origin, referer):
        raise fastapi.HTTPException(status_code=403, detail=f"EAP-3001 域名不在白名单: {origin or referer}")

    if not await rate_limiter.allow_async(f"embed:{channel.id}"):
        raise fastapi.HTTPException(status_code=429, detail="EAP-2001 请求过于频繁")

    session_token = sign_session(
        agent=channel.agent_name, tenant_id=_tenant_id_of(db, token),
        user_id=body.user_id, channel_id=channel.id,
    )
    from ...config import get_settings

    return {
        "session_token": session_token,
        "token_type": "bearer",
        "expires_in": get_settings().embed_session_ttl,
        "agent": channel.agent_name,
    }


def _tenant_id_of(db: Session, token: str) -> int:
    """M1：EmbedChannel 归属首个租户（多租户版在 channel 上加 tenant_id，docs/05 §2）。"""
    from ...models import Tenant

    tenant = db.scalar(select(Tenant))
    return tenant.id if tenant else 0

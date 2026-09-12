"""API 依赖：凭证体系（docs/07 §1）—— API Key（服务间）+ Embed 会话令牌（外链专用、最小权限）。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.security import PREFIX_SESSION, verify_session
from ..config import get_settings
from ..db import get_db
from ..modelhub.router import hub
from ..models import ApiKey, Tenant

security = fastapi.Security(fastapi.security.APIKeyHeader(name="Authorization", auto_error=False))


def resolve_tenant(
    request: fastapi.Request,
    db: Session = fastapi.Depends(get_db),
    authorization: str | None = fastapi.Security(fastapi.security.APIKeyHeader(name="Authorization", auto_error=False)),
) -> Tenant:
    """Bearer 凭证二选一：

    - API Key（服务间/控制台）：全量权限
    - 会话令牌 eap_sess_（嵌入外链）：仅限绑定智能体的 invocations（最小权限，docs/06 §2）
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise fastapi.HTTPException(status_code=401, detail="EAP-1001 缺失凭证")

    # ① 嵌入会话令牌
    if token.startswith(PREFIX_SESSION):
        payload = verify_session(token)
        if payload is None:
            raise fastapi.HTTPException(status_code=401, detail="EAP-1002 会话令牌无效或已过期")
        tenant = db.get(Tenant, payload.get("tenant_id", 0))
        if tenant is None:
            raise fastapi.HTTPException(status_code=401, detail="EAP-1001 会话租户不存在")
        request.state.auth_kind = "embed_session"
        request.state.embed_agent = payload.get("agent", "")
        request.state.tenant_id = tenant.id
        return tenant

    # ② API Key
    record = db.scalar(select(ApiKey).where(ApiKey.key == token, ApiKey.enabled == True))  # noqa: E712
    if record:
        tenant = db.get(Tenant, record.tenant_id)
        if tenant:
            request.state.auth_kind = "api_key"
            request.state.tenant_id = tenant.id
            return tenant
    raise fastapi.HTTPException(status_code=401, detail="EAP-1001 无效 API Key")


def require_api_key(request: fastapi.Request) -> None:
    """仅 API Key 可访问（KB 检索/模型管理/chat 端点；会话令牌只允许 agent 调用）。"""
    if getattr(request.state, "auth_kind", "") != "api_key":
        raise fastapi.HTTPException(status_code=403, detail="EAP-3003 会话令牌无此权限（最小权限边界）")


__all__ = ["resolve_tenant", "require_api_key", "get_db", "hub", "get_settings"]

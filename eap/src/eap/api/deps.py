"""API 依赖：凭证体系（docs/07 §1 + M6 资源服务器模式）三轨：

- API Key（服务间/控制台）：全量权限
- 会话令牌 eap_sess_（嵌入外链）：仅限绑定智能体的 invocations（最小权限）
- 外部 IdP JWT（用户身份，租户系统签发）：JWKS RS256 验签 → claims 映射租户/用户/角色
"""

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


def _resolve_jwt_tenant(db: Session, claims: dict) -> Tenant | None:
    """claims.tenant_id/tid → Tenant。数字声明按主键；字符串按租户名匹配。"""
    from ..runtime.oidc import extract_identity

    identity = extract_identity(claims)
    key = identity["tenant_key"]
    if key is None:
        return None
    if isinstance(key, int):
        return db.get(Tenant, key)
    return db.scalar(select(Tenant).where(Tenant.name == str(key)))


def resolve_tenant(
    request: fastapi.Request,
    db: Session = fastapi.Depends(get_db),
    authorization: str | None = fastapi.Security(fastapi.security.APIKeyHeader(name="Authorization", auto_error=False)),
) -> Tenant:
    """Bearer 凭证三轨：嵌入会话令牌 → API Key → 外部 IdP JWT（资源服务器）。

    JWT 通道把租户写入请求级会话变量（PostgreSQL RLS 行级隔离依据，db.get_db 消费）。
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

    # ② API Key：优先 key_hash（M7 哈希存储）；兼容期回退明文列（存量未迁移凭证）
    from ..security_keys import key_hash

    record = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash(token),
                                            ApiKey.enabled == True))  # noqa: E712
    if record is None:
        record = db.scalar(select(ApiKey).where(ApiKey.key == token,
                                                ApiKey.enabled == True))  # noqa: E712
    if record:
        tenant = db.get(Tenant, record.tenant_id)
        if tenant:
            request.state.auth_kind = "api_key"
            request.state.tenant_id = tenant.id
            return tenant

    # ③ 外部 IdP JWT（形如 JWT 且未命中本地凭证时尝试；OIDC 未配置则跳过）
    if token.count(".") == 2:
        from ..runtime import oidc
        from ..security_keys import is_revoked

        if oidc.configured():
            if is_revoked(token):
                raise fastapi.HTTPException(status_code=401, detail="EAP-1002 token 已被吊销")
            try:
                claims = oidc.verify_access_token(token)
            except oidc.OIDCError as e:
                raise fastapi.HTTPException(status_code=401, detail=str(e)) from e
            tenant = _resolve_jwt_tenant(db, claims)
            if tenant is None:
                raise fastapi.HTTPException(
                    status_code=403, detail="EAP-1004 token 声明未映射到平台租户（tenant_id/tid）")
            identity = oidc.extract_identity(claims)
            request.state.auth_kind = "jwt"
            request.state.tenant_id = tenant.id
            request.state.user = identity["user"]
            request.state.roles = identity["roles"]
            # PostgreSQL RLS（M9）：会话变量为行级隔离依据（策略 fail-closed）
            if db.get_bind().dialect.name == "postgresql":
                from sqlalchemy import text as _text

                db.execute(_text("SET LOCAL eap.tenant_id = :t"), {"t": str(tenant.id)})
            return tenant

    raise fastapi.HTTPException(status_code=401, detail="EAP-1001 无效 API Key")


def require_api_key(request: fastapi.Request) -> None:
    """仅 API Key 可访问（KB 检索/模型管理/chat 端点；会话令牌只允许 agent 调用）。"""
    if getattr(request.state, "auth_kind", "") not in ("api_key", "jwt"):
        raise fastapi.HTTPException(status_code=403, detail="EAP-3003 会话令牌无此权限（最小权限边界）")


def require_admin(request: fastapi.Request) -> None:
    """管理面写操作守卫（M14 RBAC 最小实现）：

    - API Key 通道 = 服务间运维身份 → 放行（与现有全量语义一致）
    - JWT 通道 → roles 须含 admin（claim 映射见 oidc.extract_identity）；member 只读
    - 身份微服务迁出后由其承担角色判定，本守卫换调用方即可
    """
    kind = getattr(request.state, "auth_kind", "")
    if kind == "api_key":
        return
    if kind == "jwt":
        roles = getattr(request.state, "roles", [])
        if "admin" in roles:
            return
        raise fastapi.HTTPException(status_code=403, detail="EAP-3005 需要 admin 角色")
    raise fastapi.HTTPException(status_code=403, detail="EAP-3003 管理操作需要 API Key 或 admin JWT")


__all__ = ["resolve_tenant", "require_api_key", "require_admin", "get_db", "hub", "get_settings"]

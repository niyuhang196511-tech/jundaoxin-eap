"""API 依赖：凭证体系（docs/07 §1 + M6 资源服务器模式）三轨：

- API Key（服务间/控制台）：全量权限
- 会话令牌 eap_sess_（嵌入外链）：仅限绑定智能体的 invocations（最小权限）
- 外部 IdP JWT（用户身份，租户系统签发）：JWKS RS256 验签 → claims 映射租户/用户/角色

M51-C RLS 角色收敛：JWT 通道在事务内追加 SET LOCAL ROLE <EAP_DB_APP_ROLE>
（非 owner 应用角色，迁移 c7e9b2d4f6a8 创建 eap_app），使 M47/M50 的 RLS 策略
对租户通道真实生效（owner/超级用户默认豁免 → 收敛前策略从不被评估）。
API Key/embed/worker 路径零改动——平台身份（owner 豁免）不变。
"""

from __future__ import annotations

import re

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.security import PREFIX_SESSION, verify_session
from ..config import get_settings
from ..db import get_db
from ..modelhub.router import hub
from ..models import ApiKey, Tenant

security = fastapi.Security(fastapi.security.APIKeyHeader(name="Authorization", auto_error=False))

# M51-C：SET LOCAL ROLE 的角色名白名单。SET ROLE 与 SET 同款不支持绑定参数
# （psycopg 服务端绑定渲染为 $1 → syntax error，见 resolve_tenant 内 M49-C 注释），
# 角色名只能以字面量内联进语句——故对配置值做严格校验：仅小写 PG 常规标识符
# （^[a-z_][a-z0-9_]{0,62}$，≤63 字节 NAMEDATALEN 上限）放行，其余一律拒绝
# （fail-closed 抛错，绝不静默跳过——静默跳过等于悄悄回退 owner、RLS 失效）。
# 内联值仅可能是通过校验的配置值本身，对齐 rls.py:29-30 的 DDL 静态字面量纪律（零注入面）。
_APP_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _set_local_role_sql(role: str) -> str:
    """构造 JWT 通道的 SET LOCAL ROLE 语句（角色名经 _APP_ROLE_RE 白名单校验）。

    非法角色名抛 ValueError（resolve_tenant 转 500）——配置错误必须显性失败。
    """
    if not _APP_ROLE_RE.fullmatch(role):
        raise ValueError(
            f"EAP_DB_APP_ROLE 非法: {role!r}（仅允许 ^[a-z_][a-z0-9_]{{0,62}}$）")
    return f"SET LOCAL ROLE {role}"


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

    JWT 通道把租户写入请求级会话变量（PostgreSQL RLS 行级隔离依据，db.get_db 消费）；
    M51-C 起当 EAP_DB_APP_ROLE 非空时同事务内追加 SET LOCAL ROLE 收敛为非 owner
    应用角色（RLS 策略真实生效）。作用域与既有语义（M49-C 登记）：

    - set_config(..., true) 与 SET LOCAL ROLE 均为**事务级**：请求会话 commit/
      rollback 后一并失效——GUC 在池化连接上残留为 ''（NULLIF 谓词下 fail-closed，
      迁移 e4a8c2f6b9d1），角色自动恢复为登录角色（部署形态 = 表 owner）。
    - 因此端点在**同一请求内 commit 之后**的后续查询回到 owner 身份：GUC 丢失且
      RLS 不再约束（owner 豁免）。这是既有 GUC 语义的自然延伸，本改动如实登记、
      不扩大改动面（现有端点的 commit 后查询多为审计/用量落库，走独立会话的
      平台身份路径，本就不受请求事务影响）。
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
        _set_acl(request, tenant.id)
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
            _set_acl(request, tenant.id)
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
            _set_acl(request, tenant.id)
            # PostgreSQL RLS（M9）：会话变量为行级隔离依据（策略 fail-closed）。
            # M49-C PG 实测修正：SET 语句不支持绑定参数（psycopg 服务端绑定渲染为
            # $1 → syntax error），改用等价函数 set_config(name, value, is_local=true)
            # ——与 SET LOCAL 同为事务级作用域，且支持参数化。
            if db.get_bind().dialect.name == "postgresql":
                from sqlalchemy import text as _text

                db.execute(_text("SELECT set_config('eap.tenant_id', :t, true)"),
                           {"t": str(tenant.id)})
                # M51-C RLS 角色收敛：EAP_DB_APP_ROLE 非空时同事务内 SET LOCAL ROLE
                # 收敛为非 owner 应用角色（迁移 c7e9b2d4f6a8 创建 eap_app 并授应用级
                # DML）——owner/超级用户对 RLS 默认豁免，收敛前策略从不被评估；收敛后
                # JWT 租户通道按上方 GUC 真实过滤。事务级作用域（commit/rollback 后
                # 恢复登录角色），与 set_config(..., true) 生命周期一致；commit 后的
                # 同请求后续查询回到 owner（RLS 不约束），既有 GUC 语义的如实延伸，
                # 见 resolve_tenant docstring。角色名白名单校验见 _APP_ROLE_RE——
                # SET ROLE 不支持绑定参数，只能字面量内联（零注入面纪律同 rls.py）。
                app_role = get_settings().db_app_role
                if app_role:
                    try:
                        role_sql = _set_local_role_sql(app_role)
                    except ValueError as e:
                        raise fastapi.HTTPException(
                            status_code=500, detail=f"EAP-1005 {e}") from e
                    db.execute(_text(role_sql))
            return tenant

    raise fastapi.HTTPException(status_code=401, detail="EAP-1001 无效 API Key")


def _set_acl(request: fastapi.Request, tenant_id: int) -> None:
    """检索 ACL 主体上下文（M36/L6）下发——resolve_tenant 单点覆盖三通道：

    JWT → user/roles 原样；API Key → 平台管理员语义（roles=["admin"]，与 require_admin
    判定一致）；嵌入会话 → end-user 语义（roles=["embed"]，无平台身份）。
    """
    from ..knowledge.acl import AclContext, set_acl_context

    if request.state.auth_kind == "jwt":
        ctx = AclContext(tenant_id=tenant_id, user_id=str(getattr(request.state, "user", "") or ""),
                         roles=tuple(getattr(request.state, "roles", []) or []))
    elif request.state.auth_kind == "api_key":
        ctx = AclContext(tenant_id=tenant_id, roles=("admin",))
    else:  # embed_session
        ctx = AclContext(tenant_id=tenant_id, roles=("embed",))
    set_acl_context(ctx)


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

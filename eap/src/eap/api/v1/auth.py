"""OIDC/SSO 认证 API（docs/06 §1，M3）：login 重定向 + callback 换发租户 API Key。

- 未配置 EAP_OIDC_ISSUER 时端点返回 501（平台回退纯 API Key 模式）
- callback：授权码 → id_token（验签）→ upsert 用户（OIDC sub 定位）→ 换发/复用 API Key
- 租户映射：MVP 全部落默认租户（多租户映射见 docs/05 §5）
"""

from __future__ import annotations

import fastapi
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import ApiKey, UserRecord
from ...observability import audit
from ...runtime import oidc
from ...config import get_settings
from ..deps import resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/auth", dependencies=[fastapi.Depends(get_db)])


@router.get("/oidc/login")
def oidc_login():
    """发起 SSO：307 跳转 IdP 授权页（state 由 IdP/前端回传校验，MVP 透传）。"""
    if not oidc.configured():
        raise fastapi.HTTPException(status_code=501,
                                    detail="EAP-1101 OIDC 未配置（EAP_OIDC_ISSUER / EAP_OIDC_CLIENT_ID）")
    import secrets

    state = secrets.token_urlsafe(16)
    return RedirectResponse(oidc.auth_url(state), status_code=307)


@router.get("/oidc/callback")
def oidc_callback(code: str, db: Session = fastapi.Depends(get_db)):
    """授权回调：换码验签 → 用户 upsert → API Key 换发（已有则复用）。"""
    if not oidc.configured():
        raise fastapi.HTTPException(status_code=501, detail="EAP-1101 OIDC 未配置")
    try:
        id_token = oidc.exchange_code(code)
        claims = oidc.verify_id_token(id_token)
    except oidc.OIDCError as e:
        raise fastapi.HTTPException(status_code=401, detail=str(e)) from e

    sub = claims["sub"]
    user = db.scalar(select(UserRecord).where(UserRecord.sub == sub))
    if user is None:
        user = UserRecord(sub=sub, email=str(claims.get("email") or ""),
                          name=str(claims.get("name") or ""))
        db.add(user)
        db.flush()
    # API Key：按 sub 复用，丢失/停用则重新签发
    note = f"oidc:{sub}"
    key = db.scalar(select(ApiKey).where(ApiKey.note == note, ApiKey.enabled == True))  # noqa: E712
    plain = key.key if key is not None else None  # 复用场景无法还原明文（哈希存储）→ 仅新签发可见
    if key is None:
        import secrets as _secrets

        from ...security_keys import key_hash
        plain = f"eap_u_{_secrets.token_urlsafe(24)}"
        key = ApiKey(key_hash=key_hash(plain), tenant_id=user.tenant_id, note=note)
        db.add(key)
        db.flush()
        # 复用 dev 双列语义：非 dev key 不落明文（key 列置空，认证走 key_hash）
        key.key = None
    db.commit()
    return {"api_key": plain, "user": {"sub": user.sub, "email": user.email,
                                       "name": user.name, "tenant_id": user.tenant_id},
            "issuer": get_settings().oidc_issuer}


class _RevokeBody(fastapi.Request):
    pass


@router.post("/jwt/revoke")
async def revoke_jwt(request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """吊销当前请求的 JWT（M10）：管理端点——带有效 JWT 调用即吊销自身（登出/失窃处置）。

    身份微服务迁出后由身份服务提供等价能力；本端点保持兼容代理。
    """
    from datetime import datetime

    from ...security_keys import revoke_token

    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token.count(".") != 2 or not oidc.configured():
        raise fastapi.HTTPException(status_code=400, detail="EAP-1104 无可吊销的 JWT")
    # 过期时刻取自 claims（exp），由验签保证真实性——这里轻解析即可
    claims = oidc.verify_access_token(token)
    expires_at = datetime.utcfromtimestamp(int(claims.get("exp", 0)))
    revoke_token(token, expires_at, reason="self-revoke")
    return {"revoked": True, "expires_at": expires_at.isoformat() + "Z"}


# ---------- 设备绑定（M37 Harness）：员工为桌面端签发/吊销专属设备 Key ----------

@router.get("/devices", dependencies=[fastapi.Depends(resolve_tenant)])
def list_devices(request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """本租户的 Harness 设备列表（设备名/状态/签发时间；明文 key 不可见）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise fastapi.HTTPException(status_code=403, detail="EAP-3003 仅凭证通道可见设备列表")
    rows = db.scalars(select(ApiKey).where(
        ApiKey.tenant_id == tenant_id,
        ApiKey.note.like("harness:%"),  # noqa: E712
        ApiKey.enabled == True)).all()  # noqa: E712
    return [{"name": r.note.removeprefix("harness:"), "created_at": str(r.created_at) if hasattr(r, "created_at") else ""}
            for r in rows]


@router.post("/devices", dependencies=[fastapi.Depends(resolve_tenant)])
def register_device(body: dict, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """为当前租户签发 Harness 设备专属 API Key（明文仅本次返回，落库只存哈希）。

    设备名全局唯一（note = harness:<name>，唯一索引保障）；重名且在用 → 409
    （吊销后可重用同名）。审计 harness.device.register。
    """
    import secrets as _secrets

    name = str((body or {}).get("name") or "").strip()
    import re as _re

    if not _re.fullmatch(r"[a-zA-Z0-9_-]{2,40}", name):
        raise fastapi.HTTPException(status_code=422, detail="EAP-4000 设备名须为 2~40 位字母数字-_")
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise fastapi.HTTPException(status_code=403, detail="EAP-3003 仅凭证通道可注册设备")
    note = f"harness:{name}"
    if db.scalar(select(ApiKey).where(ApiKey.note == note, ApiKey.enabled == True)):  # noqa: E712
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 设备 {name} 已注册（先吊销可重用同名）")
    plain = f"eap_d_{_secrets.token_urlsafe(24)}"
    from ...security_keys import key_hash

    key = ApiKey(key_hash=key_hash(plain), tenant_id=tenant_id, note=note)
    db.add(key)
    db.commit()
    audit.record("harness.device.register", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "api_key": plain, "note": "明文仅此一次返回，请存入 Harness 凭据库"}


@router.delete("/devices/{name}", dependencies=[fastapi.Depends(resolve_tenant)])
def revoke_device(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """吊销设备 Key（enabled=False，审计 harness.device.revoke；远程吊销地基）。"""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise fastapi.HTTPException(status_code=403, detail="EAP-3003 仅凭证通道可吊销设备")
    note = f"harness:{name}"
    key = db.scalar(select(ApiKey).where(ApiKey.note == note, ApiKey.enabled == True))  # noqa: E712
    if key is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 设备 {name} 不存在或已吊销")
    key.enabled = False
    db.commit()
    audit.record("harness.device.revoke", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "status": "revoked"}

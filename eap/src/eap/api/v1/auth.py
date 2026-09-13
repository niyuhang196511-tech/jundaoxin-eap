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
from ...runtime import oidc
from ...config import get_settings

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
    if key is None:
        import secrets as _secrets

        key = ApiKey(key=f"eap_u_{_secrets.token_urlsafe(24)}", tenant_id=user.tenant_id, note=note)
        db.add(key)
    db.commit()
    return {"api_key": key.key, "user": {"sub": user.sub, "email": user.email,
                                         "name": user.name, "tenant_id": user.tenant_id},
            "issuer": get_settings().oidc_issuer}

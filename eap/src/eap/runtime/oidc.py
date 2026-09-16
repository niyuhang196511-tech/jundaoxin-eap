"""OIDC/SSO 客户端（docs/06 §1，M3）：授权码流程 + RS256 ID Token 验签（JWKS）。

可离线测试：所有外部 HTTP 收敛在 _http_get/_http_post，测试注入模拟 IdP
（discovery / token / jwks 三个端点）即可端到端跑通。
生产：配置 EAP_OIDC_ISSUER / EAP_OIDC_CLIENT_ID / EAP_OIDC_CLIENT_SECRET 即启用。
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlparse

import httpx

from ..config import get_settings

_CACHE: dict[str, dict] = {}  # issuer → discovery/jwks 缓存（进程级，TTL 语义见 M3 文档）


class OIDCError(RuntimeError):
    """OIDC 流程/令牌校验失败（EAP-1102）/ 端点越界（EAP-1103）。"""


def _validate_url(url: str) -> None:
    """SSRF 边界：仅 http(s)，且端点主机必须与已配置 issuer 同源
    （OIDC 规范本身要求 discovery 派生端点与 issuer 同源，此处双重强制）。"""
    issuer_host = urlparse(get_settings().oidc_issuer or "").hostname
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise OIDCError(f"EAP-1103 仅允许 http(s) 的 IdP 端点，收到 {parsed.scheme!r}")
    if issuer_host and parsed.hostname != issuer_host:
        raise OIDCError(f"EAP-1103 IdP 端点主机 {parsed.hostname!r} 与 issuer({issuer_host!r}) 不一致")


# ---------- HTTP 封装（测试注入点） ----------

def _http_get(url: str) -> dict:
    _validate_url(url)
    resp = httpx.get(url, timeout=10, follow_redirects=False)
    resp.raise_for_status()
    return resp.json()


def _http_post(url: str, data: dict) -> dict:
    _validate_url(url)
    resp = httpx.post(url, data=data, timeout=10, follow_redirects=False)
    resp.raise_for_status()
    return resp.json()


# ---------- 流程 ----------

def configured() -> bool:
    s = get_settings()
    return bool(s.oidc_issuer and s.oidc_client_id)


def discovery() -> dict:
    s = get_settings()
    cached = _CACHE.get("discovery")
    if cached and cached.get("issuer") == s.oidc_issuer:
        return cached
    doc = _http_get(f"{s.oidc_issuer.rstrip('/')}/.well-known/openid-configuration")
    _CACHE["discovery"] = doc
    return doc


def auth_url(state: str) -> str:
    s = get_settings()
    doc = discovery()
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return (f"{doc['authorization_endpoint']}{sep}response_type=code"
            f"&client_id={s.oidc_client_id}&redirect_uri={s.oidc_redirect_uri}"
            f"&scope=openid%20profile%20email&state={state}")


def exchange_code(code: str) -> str:
    """授权码 → id_token。"""
    s = get_settings()
    doc = discovery()
    token = _http_post(doc["token_endpoint"], {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": s.oidc_redirect_uri,
        "client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret or "",
    })
    id_token = token.get("id_token")
    if not id_token:
        raise OIDCError("EAP-1102 IdP 未返回 id_token")
    return id_token


def _b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _jwks() -> dict:
    cached = _CACHE.get("jwks")
    if cached:
        return cached
    doc = discovery()
    jwks = _http_get(doc["jwks_uri"])
    _CACHE["jwks"] = jwks
    return jwks


def _rsa_public_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    def _int(field: str) -> int:
        return int.from_bytes(_b64url(jwk[field]), "big")

    return RSAPublicNumbers(_int("e"), _int("n")).public_key()


def verify_id_token(id_token: str) -> dict:
    """RS256 验签 + iss/aud/exp 校验；通过返回 claims，失败抛 OIDCError。

    注：PKCS#1 v1.5 是 JWT RS256（RFC 7518 §3.1）规定的唯一签名填充——这是与
    所有标准 IdP 互操作的协议要求；padding oracle 风险针对 RSA 加密场景，
    验签（公开密钥、公开消息）不适用。
    """
    s = get_settings()
    try:
        head_b64, payload_b64, sig_b64 = id_token.split(".")
        header = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise OIDCError(f"EAP-1102 ID Token 格式无效: {e}") from e
    if header.get("alg") != "RS256":
        raise OIDCError(f"EAP-1102 不支持的签名算法 {header.get('alg')!r}（仅 RS256）")

    kid = header.get("kid")
    jwk = next((k for k in _jwks().get("keys", [])
                if k.get("kid") == kid and k.get("kty") == "RSA"), None)
    if jwk is None:
        raise OIDCError(f"EAP-1102 JWKS 中找不到 kid={kid!r}")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signing_input = f"{head_b64}.{payload_b64}".encode()
    try:
        _rsa_public_key(jwk).verify(_b64url(sig_b64), signing_input,
                                    padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as e:
        raise OIDCError("EAP-1102 ID Token 签名校验失败") from e

    now = int(time.time())
    if claims.get("exp", 0) < now:
        raise OIDCError("EAP-1102 ID Token 已过期")
    if s.oidc_issuer and claims.get("iss") != s.oidc_issuer.rstrip("/"):
        raise OIDCError(f"EAP-1102 iss 不匹配: {claims.get('iss')!r}")
    if claims.get("aud") not in (s.oidc_client_id, [s.oidc_client_id]):
        raise OIDCError(f"EAP-1102 aud 不匹配: {claims.get('aud')!r}")
    if not claims.get("sub"):
        raise OIDCError("EAP-1102 ID Token 缺少 sub")
    return claims


def verify_access_token(token: str) -> dict:
    """外部 IdP 签发的 access token（JWT）验签 —— 资源服务器模式（扩展开发体系 / M6 租户集成）。

    与 verify_id_token 同一 JWKS/RS256 机制，但不校验 aud=client_id（access token 的
    aud 指向资源服务器，可经 EAP_OIDC_JWT_AUDIENCE 显式约束）；返回 claims：
    tenant 取 tenant_id/tid 声明，用户取 sub，角色取 roles/realm_access.roles。
    """
    s = get_settings()
    try:
        head_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise OIDCError(f"EAP-1102 token 格式无效: {e}") from e
    if header.get("alg") != "RS256":
        raise OIDCError(f"EAP-1102 不支持的签名算法 {header.get('alg')!r}（仅 RS256）")

    kid = header.get("kid")
    jwk = next((k for k in _jwks().get("keys", [])
                if k.get("kid") == kid and k.get("kty") == "RSA"), None)
    if jwk is None:
        raise OIDCError(f"EAP-1102 JWKS 中找不到 kid={kid!r}")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signing_input = f"{head_b64}.{payload_b64}".encode()
    try:
        _rsa_public_key(jwk).verify(_b64url(sig_b64), signing_input,
                                    padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as e:
        raise OIDCError("EAP-1102 token 签名校验失败") from e

    now = int(time.time())
    if claims.get("exp", 0) < now:
        raise OIDCError("EAP-1102 token 已过期")
    if s.oidc_issuer and claims.get("iss") != s.oidc_issuer.rstrip("/"):
        raise OIDCError(f"EAP-1102 iss 不匹配: {claims.get('iss')!r}")
    if s.jwt_audience and claims.get("aud") not in (
            s.jwt_audience, [s.jwt_audience]):
        raise OIDCError(f"EAP-1102 aud 不匹配: {claims.get('aud')!r}")
    if not claims.get("sub"):
        raise OIDCError("EAP-1102 token 缺少 sub")
    return claims


def extract_identity(claims: dict) -> dict:
    """claims → {tenant_key, user, roles}（租户键支持 tenant_id/tid 声明）。"""
    roles = claims.get("roles")
    if not isinstance(roles, list):
        realm = claims.get("realm_access") or {}
        roles = realm.get("roles") or []
    return {
        "tenant_key": claims.get("tenant_id", claims.get("tid")),
        "user": claims.get("sub", ""),
        "roles": [str(r) for r in roles],
    }

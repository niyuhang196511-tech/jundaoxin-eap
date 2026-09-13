"""OIDC/SSO：模拟 IdP 端到端（discovery/换码/JWKS 验签/用户换发 API Key）+ 异常分支（docs/06 §1）。"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
ISSUER = "https://idp.example"
CLIENT_ID = "eap-console"

# ---- 模拟 IdP 密钥与签名 ----

_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "test-key-1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _int_b64(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwks() -> dict:
    pub = _RSA_KEY.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "kid": _KID, "alg": "RS256", "use": "sig",
                      "n": _int_b64(pub.n), "e": _int_b64(pub.e)}]}


def _make_id_token(claims: dict, kid: str = _KID, alg: str = "RS256") -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header = _b64url(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = _RSA_KEY.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(sig)}"


def _good_claims(**overrides) -> dict:
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-oidc-1",
              "email": "alice@corp.cn", "name": "Alice", "exp": int(time.time()) + 600,
              "iat": int(time.time())}
    claims.update(overrides)
    return claims


@pytest.fixture()
def fake_idp(monkeypatch):
    """注入模拟 IdP：discovery / token / jwks 三个端点（SSRF 同源校验对 fake issuer 成立）。"""
    from eap.config import get_settings
    from eap.runtime import oidc as oidc_rt

    monkeypatch.setenv("EAP_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("EAP_OIDC_CLIENT_ID", CLIENT_ID)
    get_settings.cache_clear()

    def fake_get(url: str) -> dict:
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks"}
        if url.endswith("/jwks"):
            return _jwks()
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url: str, data: dict) -> dict:
        if url.endswith("/token"):
            return {"id_token": FAKE_TOKENS["exchange"]}
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(oidc_rt, "_http_get", fake_get)
    monkeypatch.setattr(oidc_rt, "_http_post", fake_post)
    FAKE_TOKENS.clear()
    yield
    get_settings.cache_clear()


FAKE_TOKENS: dict[str, str] = {}


def test_oidc_login_redirect(client, fake_idp):
    r = client.get("/api/v1/auth/oidc/login", headers=HEADERS, follow_redirects=False)
    assert r.status_code == 307
    assert "/authorize" in r.headers["location"] and "state=" in r.headers["location"]


def test_oidc_callback_mints_api_key(client, fake_idp):
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims())
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "auth-code-1"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_key"].startswith("eap_u_")
    assert body["user"]["email"] == "alice@corp.cn"
    # 换发的 key 真能调用平台（租户 1 的 API Key）
    r2 = client.get("/api/v1/models", headers={"Authorization": f"Bearer {body['api_key']}"})
    assert r2.status_code == 200
    # 二次登录复用同一把 key（sub 定位用户）
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims())
    body2 = client.get("/api/v1/auth/oidc/callback", params={"code": "auth-code-2"},
                       headers=HEADERS).json()
    assert body2["api_key"] == body["api_key"]


def test_oidc_rejects_bad_tokens(client, fake_idp):
    # 错误 aud
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims(aud="other-app"))
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c"}, headers=HEADERS)
    assert r.status_code == 401 and "aud" in r.json()["detail"]
    # 过期
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims(exp=int(time.time()) - 10))
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c"}, headers=HEADERS)
    assert r.status_code == 401 and "过期" in r.json()["detail"]
    # 错误 iss
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims(iss="https://evil.example"))
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c"}, headers=HEADERS)
    assert r.status_code == 401 and "iss" in r.json()["detail"]
    # 篡改签名（用错误密钥签发）
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as _pad

    header = _b64url(json.dumps({"alg": "RS256", "kid": _KID}).encode())
    payload = _b64url(json.dumps(_good_claims()).encode())
    sig = rogue.sign(f"{header}.{payload}".encode(), _pad.PKCS1v15(), hashes.SHA256())
    FAKE_TOKENS["exchange"] = f"{header}.{payload}.{_b64url(sig)}"
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c"}, headers=HEADERS)
    assert r.status_code == 401 and "签名" in r.json()["detail"]
    # 不存在 kid
    FAKE_TOKENS["exchange"] = _make_id_token(_good_claims(), kid="rogue-kid")
    r = client.get("/api/v1/auth/oidc/callback", params={"code": "c"}, headers=HEADERS)
    assert r.status_code == 401 and "kid" in r.json()["detail"]

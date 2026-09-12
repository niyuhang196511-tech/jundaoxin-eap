"""嵌入外链安全：会话令牌签发/验证（HMAC）、域名白名单、滑动窗口限流（docs/04 §6、06 §2）。

令牌格式（无外部依赖）：
    eap_sess_<base64url(payload_json)>.<base64url(hmac_sha256)>
    payload = {agent, tenant_id, user_id?, exp, iat}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlparse

from ..config import get_settings

PREFIX_EMBED = "eap_emb_"
PREFIX_SESSION = "eap_sess_"


# ---------- 会话令牌 ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(*, agent: str, tenant_id: int, user_id: str = "", ttl: int | None = None) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {"agent": agent, "tenant_id": tenant_id, "user_id": user_id,
               "iat": now, "exp": now + (ttl or s.embed_session_ttl), "jti": uuid.uuid4().hex[:8]}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(s.session_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{PREFIX_SESSION}{body}.{sig}"


def verify_session(token: str) -> dict | None:
    """验证会话令牌：签名 + 过期。无效/过期返回 None。"""
    s = get_settings()
    if not token.startswith(PREFIX_SESSION):
        return None
    try:
        body, sig = token[len(PREFIX_SESSION):].split(".", 1)
    except ValueError:
        return None
    expect = _b64(hmac.new(s.session_secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------- 域名白名单 ----------

def domain_allowed(domains: list, origin: str | None, referer: str | None) -> bool:
    """["*"] 放行一切；否则 Origin（缺省用 Referer）的 host 需精确或后缀匹配白名单项。"""
    if "*" in (domains or []):
        return True
    raw = origin or referer
    if not raw:
        return False
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for entry in domains or []:
        e = str(entry).lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        if host == e or host.endswith("." + e.removeprefix(".")):
            return True
    return False


# ---------- 滑动窗口限流 ----------

class SlidingWindow:
    """进程内限流（单实例开发用；多副本换 Redis 滑窗，docs/03 §5）。"""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def retry_after(self, key: str) -> int:
        hits = self._hits.get(key, [])
        if not hits:
            return 0
        return max(1, int(self.window - (time.monotonic() - hits[0])))


rate_limiter = SlidingWindow(limit=get_settings().embed_rate_limit)

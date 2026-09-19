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
    """进程内限流（单实例/无 Redis 回退；多副本自动切 Redis 滑窗，docs/03 §5）。"""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._redis_url: str | None = None
        self._redis_checked = False

    def _redis(self):
        """惰性探测 Redis（EAP_REDIS_URL 配置即启用多副本限流）。"""
        import redis.asyncio as aioredis  # noqa: F401  探测仅确认配置

        if not self._redis_checked:
            self._redis_checked = True
            self._redis_url = get_settings().redis_url
        return self._redis_url

    async def allow_async(self, key: str) -> bool:
        """Redis 滑窗（MULTI 原子 ZADD+ZREM+ZCARD）；未配置 Redis 回退进程内 allow。"""
        url = self._redis()
        if not url:
            return self.allow(key)
        import time as _time

        import redis.asyncio as aioredis

        now = _time.time()
        window_start = now - self.window
        redis_key = f"eap:ratelimit:{key}"
        r = aioredis.from_url(url, decode_responses=True)
        try:
            pipe = r.pipeline(transaction=True)
            pipe.zremrangebyscore(redis_key, 0, window_start)
            pipe.zadd(redis_key, {f"{now}": now})
            pipe.zcard(redis_key)
            _, _, count = await pipe.execute()
            await r.expire(redis_key, self.window)
            return int(count) <= self.limit
        except Exception:
            return self.allow(key)  # Redis 抖动回退进程内
        finally:
            await r.aclose()

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

# 多作用域限流注册表（v0.6-⑤）：scope 名 → SlidingWindow 实例
_limiters: dict[str, SlidingWindow] = {"embed": rate_limiter}


def get_limiter(scope: str) -> SlidingWindow:
    """按作用域取限流器（首次访问按配置实例化并缓存）。"""
    if scope not in _limiters:
        from ..config import get_settings

        settings = get_settings()
        limit = getattr(settings, f"{scope}_rate_limit", None)
        if limit is None:
            limit = settings.embed_rate_limit
        _limiters[scope] = SlidingWindow(limit=int(limit))
    return _limiters[scope]

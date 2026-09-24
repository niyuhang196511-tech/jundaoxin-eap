"""嵌入外链安全：会话令牌签发/验证（HMAC）、域名白名单、滑动窗口限流（docs/04 §6、06 §2）。

令牌格式（无外部依赖）：
    eap_sess_<base64url(payload_json)>.<base64url(hmac_sha256)>
    payload = {agent, tenant_id, user_id?, exp, iat, jti, cid?}

M51-D 会话安全：
- 密钥轮换：验签走 _session_keys 多密钥回退（当前 EAP_SESSION_SECRET 优先，失败逐个试
  EAP_SESSION_SECRET_PREVIOUS）；签发恒用当前密钥——结构对齐 security_crypto._fernets（M48-C）
- 渠道级吊销：令牌绑定签发渠道（payload.cid），verify_session 回查渠道 status——
  disable 即拒（无需迁移/epoch 列；成本=每嵌入请求一次 PK 等值查询）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlparse

from sqlalchemy import select

from ..config import get_settings

PREFIX_EMBED = "eap_emb_"
PREFIX_SESSION = "eap_sess_"


# ---------- 会话令牌 ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _session_keys() -> list[str]:
    """会话签名密钥链（M51-D 密钥轮换）：当前密钥在前、旧密钥在后（顺序即验签回退顺序）。

    对齐 security_crypto._fernets（M48-C）结构；settings 在调用时读取（get_settings 的
    lru_cache 与测试 monkeypatch 兼容——测试直接替换本函数或 security.get_settings）。

    轮换流程（对齐 EAP_SECRET_KEY/EAP_SECRET_KEY_PREVIOUS 惯例）：
    1. EAP_SESSION_SECRET 换新值；
    2. 旧值放入 EAP_SESSION_SECRET_PREVIOUS（逗号分隔可多值，空白项忽略）；
    3. 存量令牌在 TTL（EAP_EMBED_SESSION_TTL，默认 2h）内验签自动回退旧密钥，自然过期；
    4. TTL 走完后移除 PREVIOUS，轮换完成。

    与 secret_key（静态密文）轮换的差异：会话令牌是短 TTL 自消亡凭证，等自然过期即可，
    无需 scripts/reencrypt_secrets.py 那样的存量重签脚本。签发恒用当前密钥（链首）。
    """
    s = get_settings()
    current = s.session_secret
    keys = [current]
    raw = s.session_secret_previous or ""
    keys.extend(k for k in (x.strip() for x in raw.split(",")) if k and k != current)
    return keys


def _channel_active(channel_id) -> bool:
    """嵌入渠道状态回查（M51-D 渠道级吊销）：仅 status == "enabled" 视为有效。

    独立短会话（与 security_keys.is_revoked 同款模式）——verify_session 的唯一消费点
    deps.resolve_tenant 不传请求级会话（deps.py 冻结面，签名不动）；PK 等值查询 O(1)，
    且该路径每请求本就有一次 Tenant 查询，增量成本可忽略。
    渠道不存在/已删、cid 畸形、DB 抖动一律按已吊销处理（fail-closed）。
    """
    from ..db import SessionLocal
    from ..models import EmbedChannel

    try:
        with SessionLocal() as db:
            status = db.scalar(select(EmbedChannel.status)
                               .where(EmbedChannel.id == int(channel_id)))
        return status == "enabled"
    except Exception:
        return False


def sign_session(*, agent: str, tenant_id: int, user_id: str = "", ttl: int | None = None,
                 channel_id: int | None = None) -> str:
    """签发会话令牌：恒用当前 EAP_SESSION_SECRET 签名（轮换期旧密钥只用于验签回退）。

    channel_id 非空时写入 payload.cid——verify_session 据此回查渠道状态实现渠道级吊销
    （渠道 disable 后其存量会话令牌立即失效）。不带 cid 的旧格式存量令牌（部署前签发）
    跳过渠道回查，在 TTL（≤2h）内自然消亡。

    jti 为令牌唯一标识（审计/排障关联用的预留字段）：当前无逐 token 吊销消费点——
    渠道级吊销 + 短 TTL 已覆盖失效路径，不另建 jti 黑名单（不强行加戏）。
    """
    s = get_settings()
    now = int(time.time())
    payload = {"agent": agent, "tenant_id": tenant_id, "user_id": user_id,
               "iat": now, "exp": now + (ttl or s.embed_session_ttl), "jti": uuid.uuid4().hex[:8]}
    if channel_id is not None:
        payload["cid"] = channel_id
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(s.session_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{PREFIX_SESSION}{body}.{sig}"


def verify_session(token: str) -> dict | None:
    """验证会话令牌：签名（多密钥回退）+ 过期 + 渠道吊销。任一不过返回 None。

    验签按 _session_keys 密钥链顺序尝试（当前密钥优先，EAP_SESSION_SECRET_PREVIOUS
    旧密钥回退——M51-D 轮换窗口期）；全部不匹配才判无效。
    """
    if not token.startswith(PREFIX_SESSION):
        return None
    try:
        body, sig = token[len(PREFIX_SESSION):].split(".", 1)
    except ValueError:
        return None
    for key in _session_keys():
        expect = _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest())
        if hmac.compare_digest(sig, expect):
            break
    else:
        return None  # 密钥链全部验签失败
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    cid = payload.get("cid")
    if cid is not None and not _channel_active(cid):
        return None  # 渠道级吊销：签发渠道已 disable/删除
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

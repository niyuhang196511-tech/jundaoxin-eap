"""MCP Server OAuth 与连接池（M34/L8，docs/10 遗留项）。

- OAuth：client_credentials 向 token_url 换取 access token，Fernet 加密落库缓存，
  过期（含 30s margin）自动重取——模式同 M31 连接器 ensure_oauth_token。
- 连接池：per (url, auth 指纹) 复用 httpx.AsyncClient（MCP streamable 传输经
  http_client= 注入），避免每次工具调用新建 TCP/TLS；token 变更（刷新）→ 指纹变化
  → 新建 client 并后台关闭旧实例。
- 动态 bearer：认证头经 httpx.Auth 每请求从 provider 读取（闭包取模块级 token 缓存），
  避免把 token 烧死在 client 默认头里。

安全：client_secret / access_token 全程 Fernet 加密落库；日志与异常不携带明文。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

_TOKEN_MARGIN = 30  # 提前 30s 视为过期，避免边界抖动
_CLIENT_LOCK = asyncio.Lock()  # 预留：并发首建时的竞态收敛（当前单事件循环内原子）

# 可注入 transport 工厂（测试 mock IdP 用）：签名 factory(token_url) → 异步上下文管理器，
# 提供 async post(url, data=...) → httpx.Response。None 时默认直连 httpx.AsyncClient。
token_transport_factory = None

# url → (auth 指纹, httpx.AsyncClient)；指纹随 token 变化，变更即换新 client
_clients: dict[str, tuple[str, object]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def get_mcp_token(db, record, force: bool = False) -> str:
    """确保 MCP Server 持有效 access token 并返回明文（过期自动重取，模式同连接器）。

    force=True 跳过缓存快路径强制重取（手动刷新端点用）。
    未配置 oauth_token_url → 返回 ""（调用方回退 api_key/无认证）。
    transport 经 record.http_client_factory 可注入便于测试（离线 mock IdP）。
    """
    from ..security_crypto import decrypt_secret

    token_url = record.oauth_token_url or ""
    if not token_url:
        return ""
    access = decrypt_secret(record.oauth_access_token_enc) or ""
    if not force and access and record.oauth_expires_at is not None \
            and record.oauth_expires_at > _utcnow() + timedelta(seconds=_TOKEN_MARGIN):
        return access
    if not record.oauth_client_id or not record.oauth_client_secret_enc:
        raise ValueError(f"EAP-7004 MCP Server {record.name} OAuth 配置不完整（需 client_id/secret）")

    from ..security_crypto import encrypt_secret

    form = {"grant_type": "client_credentials",
            "client_id": record.oauth_client_id,
            "client_secret": decrypt_secret(record.oauth_client_secret_enc) or ""}
    if record.oauth_scopes:
        form["scope"] = record.oauth_scopes
    payload = await _token_request(record, form)
    access = str(payload["access_token"])
    record.oauth_access_token_enc = encrypt_secret(access)
    expires_in = int(payload.get("expires_in") or 3600)
    record.oauth_expires_at = _utcnow() + timedelta(seconds=max(expires_in - _TOKEN_MARGIN, 1))
    db.commit()
    return access


async def _token_request(record, form: dict) -> dict:
    """向 oauth_token_url 发 token 请求（经可注入 transport），校验并返回 JSON。"""
    import httpx

    if not (record.oauth_token_url or "").startswith(("http://", "https://")):
        raise ValueError(f"EAP-7004 OAuth token_url 须为 http(s)：{record.oauth_token_url!r}")
    factory = token_transport_factory or (lambda _t: httpx.AsyncClient(timeout=10.0))
    async with factory(record.oauth_token_url) as client:
        resp = await client.post(record.oauth_token_url, data=form)
    if resp.status_code != 200:
        raise ValueError(f"EAP-7004 OAuth token 端点返回 HTTP {resp.status_code}")
    try:
        payload = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("EAP-7004 OAuth token 响应非 JSON") from e
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("EAP-7004 OAuth token 响应缺少 access_token")
    return payload


def build_mcp_headers(record, token: str) -> dict[str, str]:
    """按记录构建认证头：OAuth token 优先，回退 api_key（Fernet 解密），无则空。"""
    from ..security_crypto import decrypt_secret

    header = record.header_name or "Authorization"
    if token:
        return {header: f"Bearer {token}"}
    key = decrypt_secret(record.api_key) if record.api_key else ""
    if key:
        return {header: key}
    return {}


def get_shared_client(url: str, headers: dict[str, str]):
    """per (url, auth 指纹) 复用 httpx.AsyncClient（长连接复用，M2 遗留项落地）。

    指纹随认证头变化（token 刷新）→ 新建 client 替换，旧实例后台 aclose。
    返回 (client, fingerprint)。
    """
    import httpx

    fp = _fingerprint(json.dumps(headers, sort_keys=True))
    cached = _clients.get(url)
    if cached and cached[0] == fp:
        return cached[1], fp
    client = httpx.AsyncClient(timeout=30.0, headers=headers)
    _clients[url] = (fp, client)
    if cached is not None:
        old = cached[1]

        async def _close():
            try:
                await old.aclose()
            except Exception:
                pass
        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:
            pass  # 无事件循环上下文（测试/同步调用）：丢弃旧实例交由 GC
    return client, fp


def pool_reset() -> None:
    """测试辅助：清空连接池（进程内状态）。"""
    _clients.clear()


def close_pool() -> None:
    """进程退出钩子：同步尽力关闭池内 client（事件循环已存在时）。"""
    for _fp, client in list(_clients.values()):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(client.aclose())
        except RuntimeError:
            pass
    _clients.clear()

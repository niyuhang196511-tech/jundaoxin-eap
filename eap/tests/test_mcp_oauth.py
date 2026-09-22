"""MCP OAuth 与连接池测试（M34/L8）：token 获取/缓存/刷新、认证头构建、连接池复用。

全部离线确定性：mock IdP 经 record.http_client_factory 注入（零真实网络）；
完整 MCP 握手（load_mcp_tools 端到端）需真实 server，属 L3 式联调（见账本）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


class _MockIdP:
    """假 token 端点：记录请求、按调用序返回不同 token（验证刷新）。"""

    def __init__(self, calls: list[dict]):
        self.calls = calls

    def factory(self, url: str):
        import httpx

        captured = self.calls

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, token_url, data=None):
                captured.append({"url": token_url, "form": dict(data or {})})
                n = len(captured)
                content = json.dumps({"access_token": f"tok-{n}", "expires_in": 3600}).encode()
                return httpx.Response(200, content=content,
                                      headers={"content-type": "application/json"},
                                      request=httpx.Request("POST", token_url))

        return _Client()


def _patch_idp(monkeypatch, calls: list[dict]) -> None:
    """模块级注入 mock IdP transport（_token_request 经 token_transport_factory 取用）。"""
    from eap.runtime import mcp_auth

    monkeypatch.setattr(mcp_auth, "token_transport_factory", _MockIdP(calls).factory)


def _record(client: TestClient, name: str, calls: list[dict], *, expires_in: int | None = None):
    """注册 + 返回可按需调整过期时间的 ORM 记录（token 缓存置空）。"""
    r = client.post("/api/v1/mcp/servers", headers=HEADERS, json={
        "name": name, "url": "http://mcp.internal", "transport": "http",
        "oauth_token_url": "http://idp.internal/token",
        "oauth_client_id": "eap-client", "oauth_client_secret": "s3cret",
        "oauth_scopes": "mcp:tools"})
    assert r.status_code == 200, r.text
    from eap.db import SessionLocal
    from eap.models import MCPServerRecord

    with SessionLocal() as db:
        record = db.scalar(__import__("sqlalchemy").select(MCPServerRecord)
                           .where(MCPServerRecord.name == name))
        if expires_in is not None:
            from datetime import datetime, timedelta, timezone

            record.oauth_expires_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                                       + timedelta(seconds=expires_in))
        record.oauth_access_token_enc = None
        db.commit()
        db.refresh(record)
        return record


def test_token_fetch_cache_and_refresh(client: TestClient, monkeypatch):
    """首取走 IdP → 缓存快路径（不触网）→ force 刷新再走 IdP。"""
    from eap.runtime.mcp_auth import get_mcp_token

    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    record = _record(client, "mcp-oauth-a", calls)
    import asyncio

    with __import__("eap.db", fromlist=["SessionLocal"]).SessionLocal() as db:
        t1 = asyncio.run(get_mcp_token(db, record))
        assert t1 == "tok-1"
        assert len(calls) == 1
        # 缓存快路径：token 未过期不触网
        t2 = asyncio.run(get_mcp_token(db, record))
        assert t2 == "tok-1" and len(calls) == 1
        # 强制刷新
        t3 = asyncio.run(get_mcp_token(db, record, force=True))
        assert t3 == "tok-2" and len(calls) == 2
    # form 断言：grant/client/scopes 齐全（secret 不出现在任何日志断言面之外——此处验证传输体）
    assert calls[0]["form"]["grant_type"] == "client_credentials"
    assert calls[0]["form"]["scope"] == "mcp:tools"


def test_token_expiry_triggers_refresh(client: TestClient, monkeypatch):
    """token 已过期（margin 内）→ 自动重取。"""
    from eap.runtime.mcp_auth import get_mcp_token

    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    record = _record(client, "mcp-oauth-b", calls, expires_in=5)  # margin 30s 内 → 视为过期
    with __import__("eap.db", fromlist=["SessionLocal"]).SessionLocal() as db:
        t = asyncio.run(get_mcp_token(db, record))
    assert t == "tok-1" and len(calls) == 1


def test_build_headers_oauth_then_api_key_fallback(client: TestClient, monkeypatch):
    """OAuth token 优先；无 token 回退 api_key（Fernet 解密）；两者皆无 → 空头。"""
    from eap.runtime.mcp_auth import build_mcp_headers, get_mcp_token

    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    record = _record(client, "mcp-oauth-c", calls)
    with __import__("eap.db", fromlist=["SessionLocal"]).SessionLocal() as db:
        token = asyncio.run(get_mcp_token(db, record))
    assert build_mcp_headers(record, token) == {"Authorization": "Bearer tok-1"}

    # api_key 回退
    from eap.db import SessionLocal
    from eap.models import MCPServerRecord
    from eap.security_crypto import encrypt_secret

    with SessionLocal() as db:
        rec = db.scalar(__import__("sqlalchemy").select(MCPServerRecord)
                        .where(MCPServerRecord.name == "mcp-oauth-c"))
        rec.oauth_access_token_enc = None
        rec.oauth_token_url = ""
        rec.api_key = encrypt_secret("key-123")
        db.commit()
        db.refresh(rec)
        assert build_mcp_headers(rec, "") == {"Authorization": "key-123"}
        # 两者皆无
        rec.api_key = None
        db.commit()
        db.refresh(rec)
        assert build_mcp_headers(rec, "") == {}


def test_shared_client_pool_reuse_and_rotation(client: TestClient):
    """同 (url, 头) 复用同一 client 实例；头变化（token 刷新）→ 新实例替换。"""
    from eap.runtime.mcp_auth import get_shared_client, pool_reset

    pool_reset()
    try:
        c1, fp1 = get_shared_client("http://x.internal", {"Authorization": "Bearer a"})
        c2, fp2 = get_shared_client("http://x.internal", {"Authorization": "Bearer a"})
        assert c1 is c2 and fp1 == fp2
        c3, fp3 = get_shared_client("http://x.internal", {"Authorization": "Bearer b"})
        assert c3 is not c2 and fp3 != fp2
        # 不同 URL 各自独立
        c4, _ = get_shared_client("http://y.internal", {"Authorization": "Bearer a"})
        assert c4 is not c3
    finally:
        pool_reset()


def test_api_fields_secret_not_echoed(client: TestClient, monkeypatch):
    """注册 OAuth Server：secret Fernet 加密、列表响应不回显。"""
    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    _record(client, "mcp-oauth-d", calls)
    listing = client.get("/api/v1/mcp/servers", headers=AUTH).json()
    row = next(s for s in listing if s["name"] == "mcp-oauth-d")
    assert "oauth_client_secret" not in json.dumps(row)
    from eap.db import SessionLocal
    from eap.models import MCPServerRecord
    from eap.security_crypto import decrypt_secret
    from sqlalchemy import select

    with SessionLocal() as db:
        rec = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == "mcp-oauth-d"))
        assert decrypt_secret(rec.oauth_client_secret_enc) == "s3cret"


def test_token_refresh_endpoint(client: TestClient, monkeypatch):
    """POST /{name}/oauth/token：admin 手动刷新，响应不回显明文 token。"""
    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    _record(client, "mcp-oauth-e", calls)
    r = client.post("/api/v1/mcp/servers/mcp-oauth-e/oauth/token", headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "refreshed"
    assert "tok-" not in json.dumps(r.json())
    assert r.json()["expires_at"] not in (None, "None")
    # 非 admin 403
    r = client.post("/api/v1/mcp/servers/mcp-oauth-e/oauth/token",
                    headers={"Authorization": "Bearer dev-key-2"})
    assert r.status_code in (401, 403)
    # 未配置 OAuth 的 server → 400
    client.post("/api/v1/mcp/servers", headers=HEADERS,
                json={"name": "mcp-plain", "url": "http://mcp.internal", "transport": "http"})
    assert client.post("/api/v1/mcp/servers/mcp-plain/oauth/token",
                       headers=HEADERS).status_code == 400


def test_validate_passes_auth_headers(client: TestClient, monkeypatch):
    """validate → load_mcp_tools 携带 OAuth 认证头（假传输捕获 http_client 默认头）。"""
    from eap.runtime import mcp_client
    import mcp.client.streamable_http as _http_mod
    import mcp as _mcp

    captured: dict = {}

    class _FakeTransport:
        def __init__(self, url, http_client=None):
            captured["url"] = url
            captured["auth"] = dict(http_client.headers) if http_client is not None else None

        async def __aenter__(self):
            return object(), object()  # read/write 流占位（会话被下方假 ClientSession 接管）

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            tool = type("T", (), {"name": "ping", "description": "ping tool",
                                  "input_schema": {"type": "object", "properties": {}}})()
            return type("R", (), {"tools": [tool]})()

    monkeypatch.setattr(_http_mod, "streamable_http_client", _FakeTransport)
    monkeypatch.setattr(_mcp, "ClientSession", _FakeSession)
    calls: list[dict] = []
    _patch_idp(monkeypatch, calls)
    _record(client, "mcp-oauth-f", calls)
    r = client.post("/api/v1/mcp/servers/mcp-oauth-f/validate", headers=HEADERS)
    assert r.json()["status"] == "verified", r.text
    # httpx 内部规范化头名为小写（Headers 大小写不敏感）
    assert captured["auth"].get("authorization") == "Bearer tok-1"

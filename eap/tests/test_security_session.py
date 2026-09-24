"""M51-D 会话安全测试：session_secret 轮换（多密钥回退）+ 嵌入令牌渠道级吊销。

风格对齐 test_security_rotation.py（M48-C）：单元级 monkeypatch 密钥来源；
集成级用 session 级共享库（client 夹具），测试行收尾还原状态（隔离纪律）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from .conftest import AUTH

ORIGIN_OK = {"Origin": "https://www.company.cn", "Referer": "https://www.company.cn/faq"}


# ---------- 工具：按指定密钥构造令牌 / 伪造 settings ----------

def _fake_settings(current: str, previous: str | None = None, ttl: int = 3600) -> SimpleNamespace:
    return SimpleNamespace(session_secret=current, session_secret_previous=previous,
                           embed_session_ttl=ttl)


def _payload(ttl: int = 3600, **extra) -> dict:
    now = int(time.time())
    return {"agent": "faq-agent", "tenant_id": 1, "user_id": "",
            "iat": now, "exp": now + ttl, "jti": "deadbeef", **extra}


def _token_signed_with(secret: str, payload: dict) -> str:
    """按 sign_session 同款格式直接用指定密钥构造 eap_sess_ 令牌（模拟轮换前签发的存量令牌）。"""
    from eap.api.security import PREFIX_SESSION, _b64

    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{PREFIX_SESSION}{body}.{sig}"


# ---------- a) session_secret 轮换（多密钥回退） ----------

def test_verify_falls_back_to_previous_session_secret(monkeypatch):
    """轮换窗口：旧密钥签发的存量令牌经 EAP_SESSION_SECRET_PREVIOUS 回退仍可验。"""
    from eap.api import security

    old, new = "old-sess-secret", "new-sess-secret"
    monkeypatch.setattr(security, "get_settings", lambda: _fake_settings(new, old))
    legacy = _token_signed_with(old, _payload())
    payload = security.verify_session(legacy)
    assert payload and payload["agent"] == "faq-agent"
    # 当前密钥签发的令牌自然也可验
    assert security.verify_session(_token_signed_with(new, _payload())) is not None


def test_sign_always_uses_current_session_secret(monkeypatch):
    """签发恒用当前密钥：轮换期新签令牌脱离旧密钥（旧密钥单独验不开）。"""
    from eap.api import security

    old, new = "old-sess-secret", "new-sess-secret"
    monkeypatch.setattr(security, "get_settings", lambda: _fake_settings(new, old))
    token = security.sign_session(agent="faq-agent", tenant_id=1, ttl=60)

    # 仅当前密钥即可验
    monkeypatch.setattr(security, "get_settings", lambda: _fake_settings(new))
    assert security.verify_session(token) is not None
    # 仅旧密钥 → 拒绝（证明签发未走回退链）
    monkeypatch.setattr(security, "get_settings", lambda: _fake_settings(old))
    assert security.verify_session(token) is None


def test_verify_rejects_unknown_secret_and_expired(monkeypatch):
    """链上全部密钥不匹配 → 拒绝；旧密钥匹配但已过期 → 仍拒绝（过期判定独立于轮换）。"""
    from eap.api import security

    monkeypatch.setattr(security, "get_settings",
                        lambda: _fake_settings("new-sess-secret", "old-sess-secret"))
    assert security.verify_session(_token_signed_with("attacker-secret", _payload())) is None
    assert security.verify_session(_token_signed_with("old-sess-secret", _payload(ttl=-1))) is None


def test_previous_multi_values_blank_entries_and_dedup(monkeypatch):
    """PREVIOUS 逗号分隔多值、空白项忽略、与当前密钥重复项去重（对齐 _fernets 构链语义）。"""
    from eap.api import security

    cur = "cur-secret"
    monkeypatch.setattr(security, "get_settings",
                        lambda: _fake_settings(cur, f" k1 ,, {cur} , k2 ,"))
    assert security._session_keys() == [cur, "k1", "k2"]
    # 链上任一旧密钥签发的存量令牌均可验
    assert security.verify_session(_token_signed_with("k1", _payload())) is not None
    assert security.verify_session(_token_signed_with("k2", _payload())) is not None


# ---------- b) 渠道级吊销 ----------

def _create_channel(client: TestClient) -> dict:
    resp = client.post("/api/v1/agents/faq-agent/embed", headers=AUTH,
                       json={"domains": ["*"], "note": "m51d-revoke"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _exchange(client: TestClient, token: str) -> str:
    resp = client.post("/api/v1/embed/session",
                       headers={"Authorization": f"Bearer {token}", **ORIGIN_OK},
                       json={"user_id": "visitor-revoke"})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_token"]


def _invoke(client: TestClient, session_token: str):
    return client.post("/api/v1/agents/faq-agent/invocations",
                       headers={"Authorization": f"Bearer {session_token}"},
                       json={"input": "如何创建知识库？"})


def test_channel_disable_revokes_session_token_immediately(client: TestClient):
    """签发 → 渠道 disable → 会话令牌立即失效（不等 TTL 自然过期）；换取入口同时仍拒。"""
    channel = _create_channel(client)
    session_token = _exchange(client, channel["token"])
    assert _invoke(client, session_token).status_code == 200

    assert client.delete(f"/api/v1/embed/{channel['channel_id']}", headers=AUTH).status_code == 200

    resp = _invoke(client, session_token)
    assert resp.status_code == 401
    assert "EAP-1002" in resp.json()["detail"]
    # EmbedToken 换取入口维持既有语义（401 EAP-1003）
    resp = client.post("/api/v1/embed/session",
                       headers={"Authorization": f"Bearer {channel['token']}", **ORIGIN_OK},
                       json={})
    assert resp.status_code == 401 and "EAP-1003" in resp.json()["detail"]


def test_channel_reenable_restores_session_token(client: TestClient):
    """如实断言：吊销=渠道状态实时回查（可逆）——重新 enable 后未过期的同一令牌立即恢复可用。"""
    from eap.db import SessionLocal
    from eap.models import EmbedChannel

    channel = _create_channel(client)
    session_token = _exchange(client, channel["token"])
    client.delete(f"/api/v1/embed/{channel['channel_id']}", headers=AUTH)
    assert _invoke(client, session_token).status_code == 401

    with SessionLocal() as db:
        row = db.get(EmbedChannel, channel["channel_id"])
        row.status = "enabled"
        db.commit()
    assert _invoke(client, session_token).status_code == 200

    # 收尾：重新 disable，不在会话级共享库留活渠道（隔离纪律）
    with SessionLocal() as db:
        db.get(EmbedChannel, channel["channel_id"]).status = "disabled"
        db.commit()


def test_api_key_channel_unaffected_by_disable(client: TestClient):
    """渠道级吊销只作用于嵌入会话令牌通道：API Key 对同一 agent 的调用不受渠道 disable 影响。"""
    channel = _create_channel(client)
    _exchange(client, channel["token"])
    client.delete(f"/api/v1/embed/{channel['channel_id']}", headers=AUTH)
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200


def test_token_bound_to_missing_channel_rejected(client: TestClient):
    """fail-closed：令牌 cid 指向不存在/已删除的渠道 → 拒绝（与 disable 同语义）。"""
    from eap.api import security

    token = security.sign_session(agent="faq-agent", tenant_id=1, ttl=60, channel_id=987654321)
    assert security.verify_session(token) is None


def test_legacy_token_without_cid_skips_channel_check(client: TestClient):
    """存量兼容：部署前签发的旧格式令牌（payload 无 cid）跳过渠道回查，TTL（≤2h）内自然消亡。"""
    from eap.api import security

    token = security.sign_session(agent="faq-agent", tenant_id=1, ttl=60)
    payload = security.verify_session(token)
    assert payload is not None and "cid" not in payload

"""嵌入外链测试：渠道管理 / 会话换取 / 域名白名单 / 最小权限越权 / Widget 静态资源。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH

ORIGIN_OK = {"Origin": "https://www.company.cn", "Referer": "https://www.company.cn/faq"}
ORIGIN_BAD = {"Origin": "https://evil.example.com"}


def _create_channel(client: TestClient, domains: list[str]) -> dict:
    resp = client.post("/api/v1/agents/faq-agent/embed", headers=AUTH,
                       json={"domains": domains, "note": "test"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _exchange(client: TestClient, token: str, headers: dict) -> dict:
    resp = client.post("/api/v1/embed/session", headers={"Authorization": f"Bearer {token}", **headers},
                       json={"user_id": "visitor-1"})
    return {"status": resp.status_code, "body": resp.json()}


def test_embed_full_flow(client: TestClient):
    """创建渠道 → 域名内换取会话 → 会话调用智能体（带引用）。"""
    channel = _create_channel(client, ["company.cn", "*.company.cn"])

    result = _exchange(client, channel["token"], ORIGIN_OK)
    assert result["status"] == 200, result
    session_token = result["body"]["session_token"]
    assert session_token.startswith("eap_sess_")
    assert result["body"]["agent"] == "faq-agent"
    assert result["body"]["expires_in"] > 0

    # 会话令牌调用绑定的智能体
    resp = client.post("/api/v1/agents/faq-agent/invocations",
                       headers={"Authorization": f"Bearer {session_token}"},
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200
    assert resp.json()["citations"], "嵌入会话调用应返回引用"


def test_embed_domain_denied(client: TestClient):
    channel = _create_channel(client, ["company.cn"])
    result = _exchange(client, channel["token"], ORIGIN_BAD)
    assert result["status"] == 403
    assert "EAP-3001" in result["body"]["detail"]


def test_embed_session_scope_limit(client: TestClient):
    """最小权限：会话令牌只能调用绑定智能体，且不能访问 KB/chat/模型管理。"""
    channel = _create_channel(client, ["*"])
    result = _exchange(client, channel["token"], ORIGIN_OK)
    session = {"Authorization": f"Bearer {result['body']['session_token']}"}

    # 越权调用其他智能体 → 403（路由层在注册表查找前先做会话绑定校验）
    resp = client.post("/api/v1/agents/sales-helper/invocations", headers=session, json={"input": "hi"})
    assert resp.status_code == 403
    assert "EAP-3002" in resp.json()["detail"]

    # KB / chat / models → 403（会话令牌无此权限）
    assert client.post("/api/v1/kb/website-faq/retrieve", headers=session,
                       json={"query": "x"}).status_code == 403
    assert client.post("/v1/chat/completions", headers=session,
                       json={"messages": [{"role": "user", "content": "x"}]}).status_code == 403
    assert client.get("/api/v1/models", headers=session).status_code == 403


def test_embed_invalid_and_disabled(client: TestClient):
    # 假 token
    result = _exchange(client, "eap_emb_not-a-real-token", ORIGIN_OK)
    assert result["status"] == 401

    # 停用渠道
    channel = _create_channel(client, ["*"])
    resp = client.delete(f"/api/v1/embed/{channel['channel_id']}", headers=AUTH)
    assert resp.status_code == 200
    result = _exchange(client, channel["token"], ORIGIN_OK)
    assert result["status"] == 401


def test_session_token_tampered(client: TestClient):
    fake = "eap_sess_eyJhZ2VudCI6ImZhcS1hZ2VudCJ9.forged-signature"
    resp = client.post("/api/v1/agents/faq-agent/invocations",
                       headers={"Authorization": f"Bearer {fake}"}, json={"input": "hi"})
    assert resp.status_code == 401


def test_widget_static_served(client: TestClient):
    resp = client.get("/sdk/eap-widget.js")
    assert resp.status_code == 200
    assert "eap-chat" in resp.text
    demo = client.get("/sdk/demo.html")
    assert demo.status_code == 200
    assert "faq-agent" in demo.text


def test_security_unit():
    """令牌签名/过期/域名单元验证。"""
    from eap.api.security import domain_allowed, sign_session, verify_session

    token = sign_session(agent="faq-agent", tenant_id=1, ttl=-1)  # 已过期
    assert verify_session(token) is None
    token2 = sign_session(agent="faq-agent", tenant_id=1, ttl=60)
    payload = verify_session(token2)
    assert payload and payload["agent"] == "faq-agent"

    assert domain_allowed(["*"], "https://anywhere.com", None) is True
    assert domain_allowed(["company.cn"], "https://a.company.cn", None) is True
    assert domain_allowed(["company.cn"], "https://notcompany.cn", None) is False
    assert domain_allowed(["company.cn"], None, None) is False

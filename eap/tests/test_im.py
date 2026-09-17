"""企业 IM 连接器：三平台推送格式、验证机制、回调→智能体→回复链路（docs/04 §4）。"""

from __future__ import annotations

import base64
import json
import time

import pytest

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
_pushed: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def capture_push(monkeypatch):
    """捕获推送（离线测试不发真实网络请求）。"""
    _pushed.clear()

    async def fake_post(url, payload, timeout=10.0):
        _pushed.append((url, payload))
        return {"status": 200, "body": {"ok": True}}

    from eap.runtime import im as im_rt

    monkeypatch.setattr(im_rt, "post_json", fake_post)


def _create_channel(client, name, platform, **kw):
    body = {"name": name, "platform": platform, "agent": "faq-agent",
            "webhook_url": f"https://hook.example/{name}", **kw}
    r = client.post("/api/v1/im/channels", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 出站格式 ----------

def test_push_payload_formats():
    from eap.runtime.im import format_push

    assert format_push("feishu", "hi") == {"msg_type": "text", "content": {"text": "hi"}}
    assert format_push("dingtalk", "hi") == {"msgtype": "text", "text": {"content": "hi"}}
    assert format_push("wecom", "hi") == {"msgtype": "text", "text": {"content": "hi"}}
    with pytest.raises(ValueError):
        format_push("slack", "hi")


# ---------- 飞书 ----------

def test_feishu_challenge_and_message_flow(client):
    _create_channel(client, "fs-1", "feishu", secret="tok-123")
    # URL 验证：原样回传 challenge
    r = client.post("/api/v1/im/feishu/fs-1/webhook",
                    json={"challenge": "ajls384", "type": "url_verification"})
    assert r.json() == {"challenge": "ajls384"}
    # token 错误 → 401
    r = client.post("/api/v1/im/feishu/fs-1/webhook",
                    json={"header": {"token": "wrong"},
                          "event": {"message": {"message_type": "text", "content": "{}"}}})
    assert r.status_code == 401
    # 正常消息 → 智能体回答推到群 webhook
    content = json.dumps({"text": "如何创建知识库？"})
    r = client.post("/api/v1/im/feishu/fs-1/webhook",
                    json={"header": {"token": "tok-123"},
                          "event": {"message": {"message_type": "text", "content": content},
                                    "sender": {"sender_id": {"open_id": "ou-1"}}}})
    assert r.status_code == 200
    assert any(url.endswith("fs-1") and "mock-llm" in str(p)
               for url, p in _pushed), _pushed


# ---------- 钉钉 ----------

def test_dingtalk_sign_and_message_flow(client):
    from eap.runtime.im import dingtalk_sign

    secret = "sec-xyz"
    _create_channel(client, "dt-1", "dingtalk", secret=secret)
    # 签名算法自洽：先算对签再请求；篡改则 401
    ts = str(int(time.time() * 1000))
    good = dingtalk_sign(secret, ts)
    assert client.post("/api/v1/im/dingtalk/dt-1/webhook",
                       headers={"timestamp": ts, "sign": "bad"},
                       json={"text": {"content": "hi"}}).status_code == 401
    r = client.post("/api/v1/im/dingtalk/dt-1/webhook",
                    headers={"timestamp": ts, "sign": good},
                    json={"text": {"content": "如何创建知识库？"},
                          "senderStaffId": "staff-9",
                          "sessionWebhook": "https://hook.example/session-1"})
    assert r.status_code == 200
    assert any(url == "https://hook.example/session-1" and "mock-llm" in str(p)
               for url, p in _pushed), _pushed


# ---------- 企业微信 ----------

def _wecom_extra():
    aes_key = base64.b64encode(b"x" * 32).decode()[:43]  # 合法 43 位 EncodingAESKey 形态
    return {"token": "qy-token", "aes_key": aes_key}


def test_wecom_echo_verification_and_message(client):
    from eap.runtime.im import wecom_decrypt, wecom_encrypt, wecom_signature

    extra = _wecom_extra()
    _create_channel(client, "wx-1", "wecom", extra=extra)
    echostr = wecom_encrypt(extra, "echo-plain-12345", "corp-1")
    params = {"msg_timestamp": "1409659813", "msg_nonce": "n1"}
    sig = wecom_signature(extra["token"], params["msg_timestamp"],
                          params["msg_nonce"], echostr)
    # 验证 URL：解密 echostr 明文回显
    r = client.get("/api/v1/im/wecom/wx-1/webhook",
                   params={**params, "msg_signature": sig, "echostr": echostr})
    assert r.status_code == 200 and r.text == "echo-plain-12345"
    # 签名错 → 401
    r = client.get("/api/v1/im/wecom/wx-1/webhook",
                   params={**params, "msg_signature": "bad", "echostr": echostr})
    assert r.status_code == 401
    # 加密消息 → 解密路由到智能体 → 推送回复
    msg_xml = "<xml><ToUserName><![CDATA[corp]]></ToUserName>" \
              "<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    secret_xml = "<xml><Content><![CDATA[如何创建知识库？]]></Content></xml>"
    encrypt = wecom_encrypt(extra, secret_xml, "corp-1")
    sig2 = wecom_signature(extra["token"], params["msg_timestamp"],
                           params["msg_nonce"], encrypt)
    r = client.post("/api/v1/im/wecom/wx-1/webhook",
                    params={**params, "msg_signature": sig2},
                    content=msg_xml.format(encrypt=encrypt),
                    headers={"Content-Type": "text/xml"})
    assert r.status_code == 200 and r.text == "success"
    assert any("mock-llm" in str(p) for _, p in _pushed), _pushed
    # 解密往返自检
    assert wecom_decrypt(extra, encrypt).startswith("<xml>")


# ---------- 渠道管理 ----------

def test_channel_management(client):
    _create_channel(client, "fs-2", "feishu")
    names = {c["name"] for c in client.get("/api/v1/im/channels", headers=AUTH).json()}
    assert {"fs-1", "dt-1", "wx-1", "fs-2"} <= names
    # 未注册智能体拒绝
    r = client.post("/api/v1/im/channels", headers=HEADERS,
                    json={"name": "fs-3", "platform": "feishu", "agent": "no-such"})
    assert r.status_code == 404
    # 未知平台拒绝
    r = client.post("/api/v1/im/channels", headers=HEADERS,
                    json={"name": "fs-4", "platform": "slack", "agent": "faq-agent"})
    assert r.status_code == 422
    # 停用后回调 409
    client.post("/api/v1/im/channels/fs-2/enabled?enabled=false", headers=HEADERS)
    r = client.post("/api/v1/im/feishu/fs-2/webhook", json={"challenge": "c1"})
    assert r.status_code == 409

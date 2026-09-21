"""IM 出站卡片与投递重试队列测试（M30 任务组 D）：三平台 send_card mock、
凭据端点加密/不回显、send-card 权限+审计+幂等、重试队列 失败→重试→成功 / 超限→dead /
入站回调失败重投。全部离线确定性：出站 HTTP 统一注入 fake，不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import IMChannelRecord, IMOutboundLogRecord

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

SENT: list[dict] = []  # 捕获的出站 HTTP 调用 {url, payload, headers}


@pytest.fixture(autouse=True)
def capture_and_fast_retry(monkeypatch):
    """捕获出站 HTTP（离线确定性）；重试参数压到测试直驱形态（后台循环不自行扫描）。"""
    SENT.clear()
    from eap.runtime import im as im_rt
    from eap.runtime import im_outbound as im_out

    async def fake_post(url, payload, headers=None, timeout=10.0):
        SENT.append({"url": url, "payload": payload, "headers": headers or {}})
        if "tenant_access_token" in url:
            return {"status": 200, "body": {"code": 0, "tenant_access_token": "tok-1"}}
        if "/im/v1/messages" in url:  # 飞书应用级消息
            return {"status": 200, "body": {"code": 0}}
        return {"status": 200, "body": {"errcode": 0}}  # 三平台机器人 webhook

    monkeypatch.setattr(im_out, "post_json", fake_post)
    monkeypatch.setattr(im_rt, "post_json", fake_post)  # 回调回复推送（runtime.im）同点注入
    monkeypatch.setattr(im_out, "max_attempts", lambda: 3)
    monkeypatch.setattr(im_out, "base_delay", lambda: 50.0)  # 入队投递不在测试窗口内到期
    monkeypatch.setattr(im_out, "max_delay", lambda: 300.0)
    monkeypatch.setattr(im_out, "poll_seconds", lambda: 3600.0)


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_channel(client, platform, prefix, **kw):
    name = _name(prefix)
    body = {"name": name, "platform": platform, "agent": "faq-agent",
            "webhook_url": f"https://hook.example/{name}", **kw}
    r = client.post("/api/v1/im/channels", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _channel_row(name: str) -> IMChannelRecord:
    with SessionLocal() as db:
        return db.scalar(select(IMChannelRecord).where(IMChannelRecord.name == name))


def _force_due(rec_id: int) -> None:
    """把投递记录置为立即到期（免等待退避，测试直驱 process_due）。"""
    with SessionLocal() as db:
        rec = db.get(IMOutboundLogRecord, rec_id)
        rec.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()


def _log_row(event_key: str) -> IMOutboundLogRecord | None:
    with SessionLocal() as db:
        return db.scalar(select(IMOutboundLogRecord).where(IMOutboundLogRecord.event_key == event_key))


# ---------- 出站卡片：三平台适配 ----------

def test_feishu_app_api_send_card(client):
    """飞书应用级 API：tenant_access_token → im/v1/messages（interactive 卡片 JSON 串）。"""
    ch = _create_channel(client, "feishu", "fs-app",
                         app_id="cli-test", app_secret="fs-secret",
                         extra={"receive_id": "ou-1", "receive_id_type": "open_id"})
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS, json={
        "title": "审批提醒", "text": "订单 **A-1** 待审批",
        "actions": [{"label": "同意", "action": "approve", "tool": "mock-erp:order",
                     "args": {"order": "A-1"}, "confirmation": "确认同意？"},
                    {"label": "驳回", "action": "reject"}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    assert len(SENT) == 2
    tok_call, msg_call = SENT
    # ① 租户令牌：应用级凭据取 token（官方 internal 端点）
    assert tok_call["url"].endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert tok_call["payload"] == {"app_id": "cli-test", "app_secret": "fs-secret"}
    # ② 发卡片：Bearer 鉴权 + interactive 卡片 + receive_id
    assert "/open-apis/im/v1/messages?receive_id_type=open_id" in msg_call["url"]
    assert msg_call["headers"]["Authorization"] == "Bearer tok-1"
    assert msg_call["payload"]["msg_type"] == "interactive"
    assert msg_call["payload"]["receive_id"] == "ou-1"
    card = json.loads(msg_call["payload"]["content"])
    assert card["header"]["title"]["content"] == "审批提醒"
    buttons = {b["value"]["action"]: b for b in card["elements"][-1]["actions"]}
    assert buttons["approve"]["value"]["tool"] == "mock-erp:order"  # Action 协议三要素透传
    assert buttons["approve"]["value"]["args"] == {"order": "A-1"}
    assert buttons["approve"]["confirm"]["title"]["content"] == "确认同意？"
    assert "reject" in buttons and "confirm" not in buttons["reject"]
    rec = _log_row(r.json()["event_key"])
    assert rec is not None and rec.direction == "out" and rec.status == "done" and rec.attempts == 1


def test_feishu_webhook_fallback_card(client):
    """未配置应用凭据：回退群机器人 webhook 直发 interactive 卡片。"""
    ch = _create_channel(client, "feishu", "fs-web")
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS,
                    json={"title": "t1", "text": "hello"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert len(SENT) == 1
    assert SENT[0]["url"] == f"https://hook.example/{ch['name']}"
    assert SENT[0]["payload"]["msg_type"] == "interactive"
    assert SENT[0]["payload"]["card"]["header"]["title"]["content"] == "t1"


def test_dingtalk_actioncard_and_sign(client):
    """钉钉机器人 actionCard：btns[].actionURL 回调可达；secret 加签按官方规则。"""
    from eap.runtime.im import dingtalk_sign

    ch = _create_channel(client, "dingtalk", "dt-card", secret="sec-dt")
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS, json={
        "title": "发布审批", "text": "版本 1.2.0 待审批",
        "actions": [{"label": "通过", "action": "approve", "tool": "mock-erp:order"}]})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert len(SENT) == 1
    url = SENT[0]["url"]
    assert url.startswith(f"https://hook.example/{ch['name']}?timestamp=")
    q = parse_qs(urlparse(url).query)
    assert q["sign"] == [dingtalk_sign("sec-dt", q["timestamp"][0])]  # 官方加签自洽
    payload = SENT[0]["payload"]
    assert payload["msgtype"] == "actionCard"
    card = payload["actionCard"]
    assert card["title"] == "发布审批" and card["btnOrientation"] == "0"
    btn = card["btns"][0]
    assert btn["title"] == "通过"
    assert f"/api/v1/im/dingtalk/{ch['name']}/webhook" in btn["actionURL"]  # 回调链路可达
    bq = parse_qs(urlparse(btn["actionURL"]).query)
    assert bq["action"] == ["approve"] and bq["tool"] == ["mock-erp:order"]


def test_wecom_template_card(client):
    """企微 template_card（button_interaction）：button key 编码 Action 协议回调引用。"""
    ch = _create_channel(client, "wecom", "wx-card")
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS, json={
        "title": "库存告警", "text": "SKU-9 低于安全库存",
        "actions": [{"label": "补货", "action": "restock", "tool": "mock-erp:restock",
                     "args": {"sku": "SKU-9"}}]})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert len(SENT) == 1
    payload = SENT[0]["payload"]
    assert payload["msgtype"] == "template_card"
    card = payload["template_card"]
    assert card["card_type"] == "button_interaction"
    assert card["main_title"]["title"] == "库存告警"
    assert card["sub_title_text"] == "SKU-9 低于安全库存"
    key = json.loads(card["button_list"][0]["key"])
    assert key == {"action": "restock", "channel": ch["name"], "tool": "mock-erp:restock",
                   "args": {"sku": "SKU-9"}}


def test_send_card_unknown_platform_rejected(client):
    _create_channel(client, "feishu", "fs-ok")  # 保证测试库就绪；本用例直驱适配器
    from eap.runtime import im_outbound as im_out
    with pytest.raises(ValueError):
        asyncio.run(im_out.send_card(type("C", (), {"platform": "slack", "name": "x",
                                                    "webhook_url": "", "secret": None,
                                                    "app_id": "", "app_secret_enc": None,
                                                    "extra": {}})(), {"title": "t"}))


# ---------- send-card 端点：权限 / 审计 / 幂等 ----------

def test_send_card_permissions_and_audit(client):
    ch = _create_channel(client, "wecom", "wx-audit")
    # 缺凭证 → 401；未知渠道 → 404
    assert client.post(f"/api/v1/im/channels/{ch['name']}/send-card",
                       json={"title": "t"}).status_code == 401
    assert client.post("/api/v1/im/channels/no-such/send-card", headers=HEADERS,
                       json={"title": "t"}).status_code == 404
    # 数字 id 路径参数等价 name
    ch_id = _channel_row(ch["name"]).id
    r = client.post(f"/api/v1/im/channels/{ch_id}/send-card", headers=HEADERS,
                    json={"title": "审计卡片", "text": "x"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    # 审计落库（action=im.send_card，target=渠道名）
    rows = client.get("/api/v1/audit", headers=AUTH, params={"action": "im.send_card"}).json()
    assert any(a["target"] == ch["name"] and a["detail"].get("title") == "审计卡片" for a in rows)
    # 停用渠道拒绝
    client.post(f"/api/v1/im/channels/{ch['name']}/enabled?enabled=false", headers=HEADERS)
    assert client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS,
                       json={"title": "t"}).status_code == 409


def test_send_card_event_key_idempotent(client):
    """同 event_key 重复投递幂等：第二次跳过不重发（IM 至少一次投递语义）。"""
    ch = _create_channel(client, "dingtalk", "dt-idem")
    key = f"evt-{uuid.uuid4().hex[:8]}"
    body = {"title": "t", "text": "x", "event_key": key}
    r1 = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS, json=body)
    r2 = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS, json=body)
    assert r1.json()["status"] == "done"
    assert r2.json()["skipped"] is True
    assert len(SENT) == 1  # 只发出一次
    assert _log_row(key) is not None


# ---------- 应用级凭据：加密存储 / 不回显 ----------

def test_credentials_created_via_channel_create_not_echoed(client, monkeypatch):
    """渠道创建携带凭据：响应不回显 secret；EAP_SECRET_KEY 配置时 Fernet 加密落库。"""
    from eap import security_crypto
    from eap.config import get_settings

    monkeypatch.setenv("EAP_SECRET_KEY", "m30-im-secret")
    get_settings.cache_clear()
    try:
        ch = _create_channel(client, "feishu", "fs-cred", app_id="cli-a", app_secret="sk-plain-1")
        assert ch["app_id"] == "cli-a" and "sk-plain-1" not in json.dumps(ch)
        row = _channel_row(ch["name"])
        assert row.app_secret_enc.startswith("enc1:")
        assert security_crypto.decrypt_secret(row.app_secret_enc) == "sk-plain-1"
    finally:
        get_settings.cache_clear()


def test_credentials_endpoint_mask_and_update(client, monkeypatch):
    """凭据端点：更新加密存储；响应掩码 *** 不回显明文；审计 im.credentials。"""
    from eap import security_crypto
    from eap.config import get_settings

    ch = _create_channel(client, "feishu", "fs-cred2")
    monkeypatch.setenv("EAP_SECRET_KEY", "m30-im-secret")
    get_settings.cache_clear()
    try:
        r = client.post(f"/api/v1/im/channels/{ch['name']}/credentials", headers=HEADERS,
                        json={"app_id": "cli-b", "app_secret": "sk-plain-2"})
        assert r.status_code == 200
        body = r.json()
        assert body["app_id"] == "cli-b" and body["app_secret"] == "***"
        assert "sk-plain-2" not in json.dumps(body)
        row = _channel_row(ch["name"])
        assert security_crypto.decrypt_secret(row.app_secret_enc) == "sk-plain-2"
        # 审计：仅记录 app_id 与是否已设密，不落明文
        rows = client.get("/api/v1/audit", headers=AUTH, params={"action": "im.credentials"}).json()
        assert any(a["target"] == ch["name"] and a["detail"] == {"app_id": "cli-b",
                                                                "secret_set": True} for a in rows)
        # 空串 = 清除
        client.post(f"/api/v1/im/channels/{ch['name']}/credentials", headers=HEADERS,
                    json={"app_id": "cli-b", "app_secret": ""})
        assert _channel_row(ch["name"]).app_secret_enc is None
    finally:
        get_settings.cache_clear()


# ---------- 重试队列：失败→重试→成功 / 超限→dead / 未到期跳过 ----------

def test_retry_fail_then_succeed(client, monkeypatch):
    from eap.runtime import im_outbound as im_out

    ch = _create_channel(client, "dingtalk", "dt-retry")
    calls = {"n": 0}

    async def flaky_post(url, payload, headers=None, timeout=10.0):
        calls["n"] += 1
        if calls["n"] == 1:  # 首投失败
            raise RuntimeError("boom-network-m30")
        SENT.append({"url": url, "payload": payload, "headers": headers or {}})
        return {"status": 200, "body": {"errcode": 0}}

    monkeypatch.setattr(im_out, "post_json", flaky_post)
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS,
                    json={"title": "t"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending" and "boom-network-m30" in r.json()["error"]
    key = r.json()["event_key"]
    rec = _log_row(key)
    assert rec.status == "pending" and rec.attempts == 1 and rec.next_retry_at is not None
    # 未到期不重投（退避窗口内扫描跳过，确定性无等待）
    assert asyncio.run(im_out.process_due()) == 0
    # 到期后重投成功：pending → done（清错误）
    _force_due(rec.id)
    assert asyncio.run(im_out.process_due()) == 1
    rec = _log_row(key)
    assert rec.status == "done" and rec.attempts == 2 and rec.error == ""
    assert len(SENT) == 1  # 重投的真实出站调用被捕获


def test_retry_exhausted_dead_letter(client, monkeypatch):
    from eap.runtime import im_outbound as im_out

    monkeypatch.setattr(im_out, "max_attempts", lambda: 2)  # 首投 + 1 次重投

    async def always_fail(url, payload, headers=None, timeout=10.0):
        raise RuntimeError("dead-m30")

    monkeypatch.setattr(im_out, "post_json", always_fail)
    ch = _create_channel(client, "wecom", "wx-dead")
    r = client.post(f"/api/v1/im/channels/{ch['name']}/send-card", headers=HEADERS,
                    json={"title": "t"})
    key = r.json()["event_key"]
    rec_id = _log_row(key).id
    _force_due(rec_id)
    assert asyncio.run(im_out.process_due()) == 1
    rec = _log_row(key)
    assert rec.status == "dead" and rec.attempts == 2 and "dead-m30" in rec.error
    # 死信不再进入扫描
    _force_due(rec_id)
    assert asyncio.run(im_out.process_due()) == 0
    assert _log_row(key).status == "dead"


def test_backoff_growth(monkeypatch):
    """指数退避：base * 2^(attempts-1)，封顶 max。"""
    from eap.runtime import im_outbound as im_out

    monkeypatch.setattr(im_out, "base_delay", lambda: 2.0)
    monkeypatch.setattr(im_out, "max_delay", lambda: 300.0)
    assert (im_out.backoff(1), im_out.backoff(2), im_out.backoff(3)) == (2.0, 4.0, 8.0)
    assert im_out.backoff(10) == 300.0


# ---------- 入站回调失败 → 重试队列（幂等重投） ----------

def test_inbound_failure_enqueued_then_retried(client, monkeypatch):
    """回调业务处理抛错（签名已通过）→ 落重试队列；重复投递同 event_key 幂等去重；
    重投成功后回复推送恢复。"""
    import eap.agents.registry as registry_mod
    from eap.runtime import im as im_rt
    from eap.runtime import im_outbound as im_out

    ch = _create_channel(client, "feishu", "fs-in", secret="tok-in")

    async def boom(db, name, request, trace_id=None):
        raise RuntimeError("agent-boom-m30")

    with monkeypatch.context() as m:
        # 注：eap.agents 包 __init__ 把 registry 名遮蔽为实例，import as 拿到的即实例本身
        m.setattr(registry_mod, "invoke", boom)
        # 同一回调投递两次（平台至少一次语义）：两条均入重试判定
        payload = {"header": {"token": "tok-in"},
                   "event": {"message": {"message_type": "text",
                                         "content": json.dumps({"text": "重投场景"}),
                                             },
                             "sender": {"sender_id": {"open_id": "ou-9"}}}}
        for _ in range(2):
            r = client.post(f"/api/v1/im/feishu/{ch['name']}/webhook", json=payload)
            assert r.status_code == 200  # 快速 2xx，错误转重试队列
    recs = (SessionLocal()
            .query(IMOutboundLogRecord)
            .filter(IMOutboundLogRecord.channel_id == _channel_row(ch["name"]).id,
                    IMOutboundLogRecord.direction == "in").all())
    assert len(recs) == 1  # 幂等：重复投递同 event_key 只落一条
    rec = recs[0]
    assert rec.status == "pending" and rec.attempts == 1
    assert rec.event_key.startswith(f"feishu:{ch['name']}:")
    assert rec.payload["text"] == "重投场景" and "agent-boom-m30" in rec.error
    # 重投：真实 registry.invoke（faq-agent mock 回答）→ 回复推到渠道 webhook
    pushed: list[tuple[str, dict]] = []

    async def fake_push(url, payload, timeout=10.0):
        pushed.append((url, payload))
        return {"status": 200, "body": {"errcode": 0}}

    monkeypatch.setattr(im_rt, "post_json", fake_push)
    _force_due(rec.id)
    assert asyncio.run(im_out.process_due()) == 1
    rec = SessionLocal().get(IMOutboundLogRecord, rec.id)
    assert rec.status == "done" and rec.attempts == 2
    assert any(url == f"https://hook.example/{ch['name']}" and "mock-llm" in str(p)
               for url, p in pushed), pushed

"""M39-B 移动远程审批与远程触发测试（docs/18 §二.5）：

- HITL 挂起 → notify_hitl 推「审批待办」卡片（按钮 value 含 task.approve + task_id）
- 卡片按钮回调（飞书 value / 钉钉 actionURL GET / 企微 EventKey）→ 批准续跑 / 拒绝否决
- /task <agent> <input...> 远程触发 → 任务引擎受理（payload._acl 快照）
- 渠道 notify_hitl 开关过滤 / event_key 幂等 / 发送失败入重试队列
全部离线确定性：出站 HTTP 统一注入 fake，任务状态用既有轮询模式。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import IMChannelRecord, IMOutboundLogRecord, TaskRecord

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

SENT: list[dict] = []  # 捕获的出站 HTTP 调用 {url, payload, headers}（卡片 + 回执）


@pytest.fixture(autouse=True)
def capture_outbound(monkeypatch):
    """捕获出站 HTTP（离线确定性）：runtime/im_outbound 与 runtime/im 同点注入。"""
    SENT.clear()
    from eap.runtime import im as im_rt
    from eap.runtime import im_outbound as im_out

    async def fake_post(url, payload, headers=None, timeout=10.0):
        SENT.append({"url": url, "payload": payload, "headers": headers or {}})
        if "tenant_access_token" in url:
            return {"status": 200, "body": {"code": 0, "tenant_access_token": "tok-1"}}
        return {"status": 200, "body": {"errcode": 0}}

    monkeypatch.setattr(im_out, "post_json", fake_post)
    monkeypatch.setattr(im_rt, "post_json", fake_post)


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_channel(client: TestClient, platform: str, prefix: str, **kw) -> dict:
    name = _name(prefix)
    body = {"name": name, "platform": platform, "agent": "faq-agent",
            "webhook_url": f"https://hook.example/{name}", **kw}
    r = client.post("/api/v1/im/channels", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _disable_notify_channels() -> None:
    """共享测试库防串扰：停用所有已开启 notify_hitl 的渠道。"""
    with SessionLocal() as db:
        for ch in db.scalars(select(IMChannelRecord)).all():
            if (ch.extra or {}).get("notify_hitl") and ch.enabled:
                ch.enabled = False
        db.commit()


def _solo_notify_channel(client: TestClient, platform: str, prefix: str, **kw) -> dict:
    """本测试专用 notify_hitl 渠道：先停用既有同类，保证推送断言不被其他渠道污染。"""
    _disable_notify_channels()
    extra = {"notify_hitl": True, **(kw.pop("extra", {}) or {})}
    return _create_channel(client, platform, prefix, extra=extra, **kw)


def _poll(client: TestClient, task_id: str, states: set[str], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH)
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in states:
            return last
        time.sleep(0.15)
    return last


def _wait_push(min_count: int = 1, timeout: float = 10.0) -> list[dict]:
    """等任务引擎 worker 发出卡片推送（挂起落库后异步推送，与状态轮询存在微小竞态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(SENT) >= min_count:
            return SENT
        time.sleep(0.1)
    return SENT


def _submit_hitl(client: TestClient, agent: str = "order-agent", text: str = "帮我下一台 EAP 一体机") -> str:
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl", "payload": {"agent": agent, "input": text}})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


def _log_row(event_key: str) -> IMOutboundLogRecord | None:
    with SessionLocal() as db:
        return db.scalar(select(IMOutboundLogRecord)
                         .where(IMOutboundLogRecord.event_key == event_key))


# ---------- HITL 挂起 → 审批卡片推送 ----------

def test_hitl_suspend_pushes_approval_card(client: TestClient):
    """agent.hitl 挂起 → notify_hitl 被调：卡片标题/摘要正确，按钮 value 含
    task.approve 保留字 + task_id + decision（扩展 Action 协议编码）。"""
    ch = _solo_notify_channel(client, "feishu", "fs-hitl")
    task_id = _submit_hitl(client)

    task = _poll(client, task_id, {"WAITING_HUMAN"})
    assert task["state"] == "WAITING_HUMAN", task["result"]
    push = _wait_push(1)
    assert len(push) == 1  # 仅 notify_hitl 渠道收到，无其他出站
    assert push[0]["url"] == f"https://hook.example/{ch['name']}"
    payload = push[0]["payload"]
    assert payload["msg_type"] == "interactive"  # 群机器人 webhook 直发卡片
    card = payload["card"]
    assert card["header"]["title"]["content"] == "审批待办"
    body_text = card["elements"][0]["text"]["content"]
    assert "order-agent" in body_text and "erp.order.create" in body_text
    buttons = {b["text"]["content"]: b["value"] for b in card["elements"][-1]["actions"]}
    assert set(buttons) == {"批准", "拒绝"}
    for label, decision in (("批准", True), ("拒绝", False)):
        value = buttons[label]
        assert value["action"] == "task.approve"  # 保留字（非工具名）
        assert value["channel"] == ch["name"]
        assert value["args"] == {"task_id": task_id, "decision": decision}
    # 投递日志：done + 幂等键 hitl:{task_id}
    rec = _log_row(f"hitl:{task_id}")
    assert rec is not None and rec.direction == "out" and rec.status == "done"
    assert rec.channel_id == _channel_id(ch["name"])


def _channel_id(name: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(IMChannelRecord.id).where(IMChannelRecord.name == name))


# ---------- 按钮回调 → 远程审批决策 ----------

def test_button_approve_via_dingtalk_action_url(client: TestClient):
    """批准：钉钉 actionCard 按钮 actionURL 点击（GET 回调）→ 引擎 approve →
    任务 PENDING 续跑完成 + 审计 harness.remote.approve + 回执「已批准」。"""
    ch = _solo_notify_channel(client, "dingtalk", "dt-approve")
    task_id = _submit_hitl(client)
    _poll(client, task_id, {"WAITING_HUMAN"})
    push = _wait_push(1)[0]

    # 从卡片按钮还原回调 URL，模拟用户点击（actionURL 即 webhook + query 引用）
    action_url = push["payload"]["actionCard"]["btns"][0]["actionURL"]
    assert "/api/v1/im/dingtalk/" in action_url and "action=task.approve" in action_url
    base = len(SENT)
    r = client.get(action_url)
    assert r.status_code == 200
    assert any("已批准" in str(p["payload"]) for p in SENT[base:])  # 回执推送
    rows = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "harness.remote.approve"}).json()
    assert any(a["target"] == task_id and a["detail"]["decision"] is True
               and a["detail"]["channel"] == ch["name"] for a in rows), rows
    task = _poll(client, task_id, {"COMPLETED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert "SO-2026-" in task["result"]["output"], "批准后应真正执行下单工具"


def test_button_deny_via_wecom_event_key(client: TestClient):
    """拒绝：企微 template_card_event 的 EventKey（XML CDATA）→ 引擎 approve(False) →
    工具被否不执行 + 回执「已拒绝」。"""
    from eap.runtime.im import wecom_encrypt, wecom_signature

    aes_key = base64.b64encode(b"x" * 32).decode()[:43]
    ch = _solo_notify_channel(client, "wecom", "wx-deny",
                              extra={"token": "qy-token", "aes_key": aes_key})
    task_id = _submit_hitl(client)
    _poll(client, task_id, {"WAITING_HUMAN"})
    _wait_push(1)

    key = json.dumps({"action": "task.approve", "channel": ch["name"],
                      "args": {"task_id": task_id, "decision": False}}, ensure_ascii=False)
    plain_xml = f"<xml><EventKey><![CDATA[{key}]]></EventKey></xml>"
    encrypt = wecom_encrypt({"token": "qy-token", "aes_key": aes_key}, plain_xml, "corp-1")
    params = {"msg_timestamp": "1409659813", "msg_nonce": "n1"}
    sig = wecom_signature("qy-token", params["msg_timestamp"], params["msg_nonce"], encrypt)
    r = client.post(f"/api/v1/im/wecom/{ch['name']}/webhook",
                    params={**params, "msg_signature": sig},
                    content=(f"<xml><ToUserName><![CDATA[corp]]></ToUserName>"
                             f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"),
                    headers={"Content-Type": "text/xml"})
    assert r.status_code == 200 and r.text == "success"
    assert any("已拒绝" in str(p["payload"]) for p in SENT[1:])  # 回执推送（首条为卡片）

    task = _poll(client, task_id, {"COMPLETED"})
    assert task["state"] == "COMPLETED"
    assert "否决" in task["result"]["output"]
    assert "SO-2026-" not in task["result"]["output"], "否决后不得执行下单"


def test_button_approve_error_replies_not_500(client: TestClient):
    """找不到任务/状态不对 → 回执错误文本（HTTP 仍 2xx）；非 task.approve 引用不拦截。"""
    ch = _create_channel(client, "feishu", "fs-err")
    webhook = f"https://hook.example/{ch['name']}"  # _create_channel 发送的 webhook_url

    def _click(value: dict):
        base = len(SENT)
        r = client.post(f"/api/v1/im/feishu/{ch['name']}/webhook",
                        json={"event": {"action": {"value": value}}})
        assert r.status_code == 200
        return [p["payload"] for p in SENT[base:]]

    # 任务不存在 → 错误回执
    replies = _click({"action": "task.approve", "channel": ch["name"],
                      "args": {"task_id": "no-such-task", "decision": True}})
    assert any("未找到任务" in str(p) for p in replies), replies
    # 状态不对（任务已 COMPLETED）→ 错误回执
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "faq-agent", "input": "hi"}})
    done_id = resp.json()["task_id"]
    _poll(client, done_id, {"COMPLETED"})
    replies = _click({"action": "task.approve", "args": {"task_id": done_id, "decision": False}})
    assert any("不在等待人工审批" in str(p) for p in replies), replies
    # 非保留字 action → 走既有链路（无文本不触发路由，不回执）
    assert _click({"action": "crm.create", "args": {"x": 1}}) == []
    # 未出现错误场景的审批审计（错误路径同样留痕，但目标非上述任务之外新增）
    rows = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "harness.remote.approve"}).json()
    assert {a["target"] for a in rows} >= {"no-such-task", done_id}
    assert webhook.startswith("https://hook.example/")  # 回执发渠道 webhook


# ---------- /task 远程触发 ----------

def test_task_prefix_remote_trigger(client: TestClient):
    """/task <agent> <input...> → 任务引擎受理 agent.invoke（payload._acl 快照），
    回执「任务已受理 {task_id}」推 sessionWebhook；任务异步执行完成。"""
    from eap.runtime.im import dingtalk_sign

    ch = _create_channel(client, "dingtalk", "dt-trig", secret="sec-trig")
    ts = str(int(time.time() * 1000))
    r = client.post(f"/api/v1/im/dingtalk/{ch['name']}/webhook",
                    headers={"timestamp": ts, "sign": dingtalk_sign("sec-trig", ts)},
                    json={"text": {"content": "/task faq-agent 如何创建知识库？"},
                          "senderStaffId": "staff-7",
                          "sessionWebhook": "https://hook.example/sess-trig"})
    assert r.status_code == 200
    receipts = [p["payload"] for p in SENT if p["url"] == "https://hook.example/sess-trig"]
    assert receipts and any("任务已受理" in str(p) for p in receipts), SENT
    m = re.search(r"任务已受理 ([0-9a-f]{32})", json.dumps(receipts, ensure_ascii=False))
    assert m, receipts
    task_id = m.group(1)

    task = _poll(client, task_id, {"COMPLETED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert "mock" in task["result"]["output"]
    assert task["type"] == "agent.invoke"
    with SessionLocal() as db:
        payload = db.get(TaskRecord, task_id).payload
    # IM 通道无登录态：_acl 快照如实记 None/空 roles（M36 任务通道同法）
    assert payload["_acl"] == {"tenant_id": None, "user_id": None, "roles": []}


def test_task_prefix_unknown_agent_rejected(client: TestClient):
    """/task 未注册智能体 → 回执错误文本，不建任务。"""
    ch = _create_channel(client, "feishu", "fs-trig-bad")
    r = client.post(f"/api/v1/im/feishu/{ch['name']}/webhook",
                    json={"header": {"token": "x"},
                          "event": {"message": {"message_type": "text",
                                                "content": json.dumps({"text": "/task no-such hi"})}}})
    assert r.status_code == 200
    assert any("未注册" in str(p["payload"]) for p in SENT), SENT
    with SessionLocal() as db:
        tasks = db.scalars(select(TaskRecord).where(TaskRecord.type == "agent.invoke")).all()
        assert all(t.payload.get("agent") != "no-such" for t in tasks)


# ---------- 渠道开关 / 幂等 / 失败入队 ----------

def test_channel_without_notify_hitl_not_pushed(client: TestClient):
    """无 notify_hitl 开关 / 渠道停用 → 挂起不推送任何出站。"""
    _disable_notify_channels()
    _create_channel(client, "feishu", "fs-nonotify")  # extra 无开关
    off = _create_channel(client, "feishu", "fs-off", extra={"notify_hitl": True})
    client.post(f"/api/v1/im/channels/{off['name']}/enabled?enabled=false", headers=HEADERS)

    task_id = _submit_hitl(client)
    task = _poll(client, task_id, {"WAITING_HUMAN"})
    assert task["state"] == "WAITING_HUMAN"
    time.sleep(1.5)  # 推送窗口：无开关渠道不应有任何出站（正常推送毫秒级发生）
    assert SENT == []


def test_notify_hitl_idempotent_event_key(client: TestClient):
    """幂等：event_key = hitl:{task_id}——同渠道重复推送跳过不重发。"""
    from eap.runtime import im_outbound as im_out

    ch = _solo_notify_channel(client, "feishu", "fs-idem")
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_hitl("task-idem-1", "order-agent",
                                                "erp.order.create", db))
    assert pushed == [ch["name"]]
    n = len(SENT)
    with SessionLocal() as db:
        pushed2 = asyncio.run(im_out.notify_hitl("task-idem-1", "order-agent",
                                                 "erp.order.create", db))
    assert pushed2 == []  # 已 done 的同键投递 → 幂等跳过
    assert len(SENT) == n
    rec = _log_row("hitl:task-idem-1")
    assert rec is not None and rec.status == "done" and rec.attempts == 1


def test_notify_hitl_send_failure_enqueued(client: TestClient, monkeypatch):
    """首投失败 → pending 入既有重试队列（enqueue），不抛出。"""
    from eap.runtime import im_outbound as im_out

    _solo_notify_channel(client, "feishu", "fs-fail")

    async def boom(url, payload, headers=None, timeout=10.0):
        raise RuntimeError("net-boom-m39")

    monkeypatch.setattr(im_out, "post_json", boom)
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_hitl("task-fail-1", "order-agent",
                                                "erp.order.create", db))
    assert len(pushed) == 1  # 渠道命中但发送失败（入队重试）
    rec = _log_row("hitl:task-fail-1")
    assert rec is not None and rec.status == "pending" and rec.attempts == 1
    assert "net-boom-m39" in rec.error and rec.next_retry_at is not None
    # 置死信：避免后续测试触发后台重试循环时对该记录发起真实出站
    with SessionLocal() as db:
        row = db.get(IMOutboundLogRecord, rec.id)
        row.status = "dead"
        db.commit()

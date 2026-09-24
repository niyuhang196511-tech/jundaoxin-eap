"""事件中心 + 触发器测试（M30）：总线发布订阅/通配、规则 CRUD 权限、事件→agent 全链路、
webhook 签名与重放去重。全部离线确定（mock 模型 + 进程内总线，无 Redis）。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_oidc import fake_idp, _make_id_token  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

ISSUER = "https://idp.example"


def _token(roles: list[str]) -> str:
    claims = {"iss": ISSUER, "sub": "trig@corp", "exp": int(time.time()) + 600,
              "tenant_id": 1, "roles": roles}
    return _make_id_token(claims)


def _poll_task(client: TestClient, task_id: str, states: set[str], timeout: float = 20.0) -> dict:
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


def _latest_fire_ref(client: TestClient, rule_name: str) -> str:
    """从审计（trigger.fire，新→旧）取某规则最近一次触发的任务引用。"""
    resp = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "trigger.fire", "target": rule_name, "limit": 20})
    assert resp.status_code == 200, resp.text
    for entry in resp.json():
        if entry["target"] == rule_name:
            return str((entry.get("detail") or {}).get("ref", ""))
    return ""


# ---------- 事件总线 ----------

def test_bus_pubsub_and_wildcard():
    """发布/订阅 + 通配：task.* 只收 task.* 前缀事件；事件结构完整。"""
    from eap.runtime.events import bus

    async def scenario():
        q_all = bus.subscribe("*")
        q_task = bus.subscribe("task.*")
        try:
            ev1 = await bus.emit("task.completed", 1, {"task_id": "t1"})
            ev2 = await bus.emit("kb.document.indexed", 1, {"kb": "kb1"})
            got = [await asyncio.wait_for(q_all.get(), 2), await asyncio.wait_for(q_all.get(), 2)]
            assert {e["id"] for e in got} == {ev1["id"], ev2["id"]}
            only_task = await asyncio.wait_for(q_task.get(), 2)
            assert only_task["type"] == "task.completed"
            assert only_task["data"] == {"task_id": "t1"}
            assert only_task["tenant_id"] == 1 and only_task["id"] == ev1["id"] and only_task["ts"] > 0
            assert q_task.empty(), "task.* 通配不应收到 kb.* 事件"
        finally:
            bus.unsubscribe(q_all)
            bus.unsubscribe(q_task)

    asyncio.run(scenario())


def test_emit_event_sync_context_no_raise():
    """无事件循环的同步上下文调用 emit_event 不抛错（离线尽力本地投递）。"""
    from eap.runtime.events import emit_event

    emit_event("kb.document.indexed", data={"kb": "no-loop-kb"})


# ---------- 规则 CRUD + 权限 ----------

def test_trigger_crud_and_rbac(client: TestClient, fake_idp):  # noqa: F811
    member = _token(roles=["member"])
    body = {"name": "m30-crud-rule", "source": "event", "event_type": "kb.document.indexed",
            "target_type": "agent", "target_name": "faq-agent"}
    # member 写 → 403（require_admin）
    resp = client.post("/api/v1/triggers", headers={"Authorization": f"Bearer {member}"}, json=body)
    assert resp.status_code == 403, resp.text
    # admin 创建 → 200；secret 不回显
    resp = client.post("/api/v1/triggers", headers=AUTH, json=body)
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["enabled"] is True and "secret" not in view and view["has_secret"] is False
    rid = view["id"]
    # 重名 → 409
    assert client.post("/api/v1/triggers", headers=AUTH, json=body).status_code == 409
    # source 语义校验：event 缺 event_type / cron 非法 → 400
    assert client.post("/api/v1/triggers", headers=AUTH,
                       json={"name": "m30-bad-evt", "source": "event",
                             "target_type": "agent", "target_name": "faq-agent"}).status_code == 400
    assert client.post("/api/v1/triggers", headers=AUTH,
                       json={"name": "m30-bad-cron", "source": "cron", "cron": "not-a-cron",
                             "target_type": "agent", "target_name": "faq-agent"}).status_code == 400
    # 列表可见
    rules = client.get("/api/v1/triggers", headers=AUTH).json()
    assert any(r["name"] == "m30-crud-rule" for r in rules)
    # member 读 → 403（列表也 admin-only）
    assert client.get("/api/v1/triggers",
                      headers={"Authorization": f"Bearer {member}"}).status_code == 403
    # PATCH 停用
    resp = client.patch(f"/api/v1/triggers/{rid}", headers=AUTH, json={"enabled": False})
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    # DELETE
    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200
    assert client.get("/api/v1/triggers", headers=AUTH).json() == [] or all(
        r["name"] != "m30-crud-rule"
        for r in client.get("/api/v1/triggers", headers=AUTH).json())


# ---------- 事件规则 → agent 全链路 ----------

def test_event_rule_fires_agent(client: TestClient):
    """事件规则全链路：总线事件 → 规则匹配 → agent.invoke 任务 → 完成（审计留痕）。"""
    resp = client.post("/api/v1/triggers", headers=AUTH, json={
        "name": "m30-evt-agent", "source": "event", "event_type": "test.hello.m30",
        "target_type": "agent", "target_name": "faq-agent"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]

    # 发射事件（测试线程无运行循环 → 经 call_soon_threadsafe 调度到应用循环）
    from eap.runtime.events import emit_event

    emit_event("test.hello.m30", data={"question": "如何创建知识库？"})

    task_id = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not task_id:
        time.sleep(0.2)
        task_id = _latest_fire_ref(client, "m30-evt-agent")
    assert task_id, "触发审计 trigger.fire 应产生任务引用"

    task = _poll_task(client, task_id, {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]

    # 清理规则（不影响其他测试）
    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200


def test_event_rule_match_filter(client: TestClient):
    """match 等值过滤：不匹配的事件不触发（无 trigger.fire 审计）。"""
    resp = client.post("/api/v1/triggers", headers=AUTH, json={
        "name": "m30-evt-match", "source": "event", "event_type": "test.match.m30",
        "match": {"kind": "wanted"}, "target_type": "agent", "target_name": "faq-agent"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]

    from eap.runtime.events import emit_event

    emit_event("test.match.m30", data={"kind": "unwanted"})
    time.sleep(1.0)  # 给引擎消费留出时间（若误触发会落审计）
    assert _latest_fire_ref(client, "m30-evt-match") == ""

    # 匹配的事件触发
    emit_event("test.match.m30", data={"kind": "wanted"})
    task_id = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not task_id:
        time.sleep(0.2)
        task_id = _latest_fire_ref(client, "m30-evt-match")
    assert task_id, "匹配事件应触发并留审计"

    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200


# ---------- webhook 触发 ----------

def test_webhook_signature_and_dedup(client: TestClient):
    """webhook：错签名 401；对签名触发 agent 任务完成；同 X-EAP-Event-Id 重放去重。"""
    resp = client.post("/api/v1/triggers", headers=AUTH, json={
        "name": "m30-hook-agent", "source": "webhook", "secret": "s3cret-key",
        "target_type": "agent", "target_name": "faq-agent"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]
    assert resp.json()["has_secret"] is True

    body = json.dumps({"question": "如何注册手写智能体？"}).encode()
    url = f"/api/v1/triggers/webhook/{rid}"
    signature = hmac.new(b"s3cret-key", body, hashlib.sha256).hexdigest()

    # 错签名 → 401
    resp = client.post(url, content=body,
                       headers={"X-EAP-Signature": "deadbeef", "Content-Type": "application/json"})
    assert resp.status_code == 401, resp.text
    # 正确签名 → accepted，触发任务
    resp = client.post(url, content=body,
                       headers={"X-EAP-Signature": signature, "X-EAP-Event-Id": "m30-evt-1",
                                "Content-Type": "application/json"})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["status"] == "submitted" and result["ref"]
    task = _poll_task(client, result["ref"], {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]

    # 同 event id 重放 → duplicate（不再创建任务）
    resp = client.post(url, content=body,
                       headers={"X-EAP-Signature": signature, "X-EAP-Event-Id": "m30-evt-1"})
    assert resp.status_code == 200 and resp.json()["status"] == "duplicate", resp.text

    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200


def test_webhook_unknown_rule_404(client: TestClient):
    """不存在/非 webhook 来源的规则 → 404。"""
    resp = client.post("/api/v1/triggers/webhook/999999", content=b"{}",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 404


def test_test_fire_endpoint(client: TestClient):
    """test-fire：admin 手工注入样例 payload，走同一 fire 通道并返回任务引用。"""
    resp = client.post("/api/v1/triggers", headers=AUTH, json={
        "name": "m30-manual", "source": "event", "event_type": "test.manual.m30",
        "target_type": "agent", "target_name": "faq-agent"})
    rid = resp.json()["id"]
    resp = client.post(f"/api/v1/triggers/{rid}/test-fire", headers=AUTH,
                       json={"data": {"question": "如何创建知识库？"}})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "submitted" and result["ref"] and result["source"] == "manual"
    task = _poll_task(client, result["ref"], {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert client.delete(f"/api/v1/triggers/{rid}", headers=AUTH).status_code == 200


def test_event_types_catalog_endpoint(client: TestClient, fake_idp):  # noqa: F811
    """GET /event-types（M49-E1）：返回事件目录，与 runtime/events.EVENT_CATALOG 一致。

    - 目录覆盖全仓已知 emit 事件类型（文档性质，非白名单）
    - 鉴权与本 router 其他 GET 一致：member 403、admin 200
    """
    from eap.runtime.events import EVENT_CATALOG

    # member 读 → 403（与 list_triggers 同 require_admin）
    member = _token(roles=["member"])
    resp = client.get("/api/v1/triggers/event-types",
                      headers={"Authorization": f"Bearer {member}"})
    assert resp.status_code == 403, resp.text
    # admin 读 → 200，目录 = EVENT_CATALOG（顺序一致）
    resp = client.get("/api/v1/triggers/event-types", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == list(EVENT_CATALOG)
    # 覆盖全仓已知实际发射的事件类型
    for known in ("agent.run.completed", "kb.document.indexed", "workflow.run.finished",
                  "connector.invoked", "task.completed", "task.failed"):
        assert known in body, f"事件目录缺 {known}"
    # webhook.test 是合成事件（不经总线/不可订阅），不应列入目录
    assert "webhook.test" not in body

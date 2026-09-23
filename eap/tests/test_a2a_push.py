"""A2A 异步任务推送挂钩测试（M45-A，任务组 E）：

- message/send + params.metadata.async_task=true（平台扩展）→ 提交任务引擎
  （agent.invoke），立即返回 working 态 Task（taskId=引擎 task_id，metadata 带
  agent/submitted），引擎里真有任务（payload 携带 agent/input/_tenant_id）
- 任务引擎终态挂点（runtime/tasks.py _notify_a2a）→ 登记过推送配置的任务
  COMPLETED/FAILED 时投递 HMAC 签名回执（completed/failed Task 快照）
- tasks/get 内存 miss → 引擎 TaskRecord 映射（8 态 → A2A 态）+ 租户隔离
- 无推送配置时不投递
全部离线确定性：出站推送注入 fake _push_post（模式同 test_a2a_v2 推送回执测试），
任务状态用 tasks/get 轮询（顺带覆盖引擎映射路径）。

诚实边界（与 a2a.py docstring 一致，不在此复测）：_A2A_PUSH 为进程内内存态，
多副本下执行副本查不到配置即不投递；回执失败仅告警不重试。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import TaskRecord

from .conftest import AUTH

RPC = "/a2a/rpc?agent=faq-agent"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离：停用全部 IM notify 渠道（本文件的 agent.invoke 异步任务会触发 M40-A
    完成回执钩子，防止任何真实 IM 出站），用例后清理 _A2A_PUSH 防跨用例残留。"""
    from eap.models import IMChannelRecord

    with SessionLocal() as db:
        for ch in db.scalars(select(IMChannelRecord)).all():
            if (ch.extra or {}).get("notify_hitl") and ch.enabled:
                ch.enabled = False
        db.commit()
    yield
    import eap.api.v1.a2a as a2a_mod

    a2a_mod._A2A_PUSH.clear()


@pytest.fixture
def push_capture(monkeypatch) -> list[dict]:
    """注入 fake _push_post（离线确定性）：捕获 A2A 推送回执出站。"""
    import eap.api.v1.a2a as a2a_mod

    captured: list[dict] = []

    async def fake_post(url, content, headers):
        captured.append({"url": url, "body": content, "headers": headers})
        return 200

    monkeypatch.setattr(a2a_mod, "_push_post", fake_post)
    return captured


def _dev_tenant_id() -> int:
    from eap.models import Tenant

    with SessionLocal() as db:
        return db.scalar(select(Tenant).where(Tenant.name == "dev")).id


def _async_send(client: TestClient, rpc_id, *, text: str = "异步问我",
                cfg: dict | None = None) -> dict:
    params: dict = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]},
                    "metadata": {"async_task": True}}
    if cfg is not None:
        params["configuration"] = {"pushNotificationConfig": cfg}
    resp = client.post(RPC, headers=AUTH,
                       json={"jsonrpc": "2.0", "id": rpc_id, "method": "message/send",
                             "params": params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def _poll_a2a_state(client: TestClient, task_id: str, states: set[str],
                    timeout: float = 10.0) -> dict:
    """轮询 tasks/get（引擎任务映射路径）直到进入目标 A2A 态。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.post(RPC, headers=AUTH, json={
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": task_id}})
        body = resp.json()
        if "result" in body:
            last = body["result"]
            if last.get("status", {}).get("state") in states:
                return last
        time.sleep(0.1)
    return last


def _wait_push(captured: list[dict], min_count: int = 1, timeout: float = 10.0) -> list[dict]:
    """等引擎 worker 投递回执（终态落库后异步推送，与状态轮询存在微小竞态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(captured) >= min_count:
            return captured
        time.sleep(0.1)
    return captured


# ---------- ① async_task=true：提交任务引擎，返回 working 态任务 ----------

def test_async_message_send_submits_engine_task(client: TestClient, push_capture):
    dev_tenant = _dev_tenant_id()
    task = _async_send(client, 1, text="异步问我")
    assert task["status"]["state"] == "working"  # 非 completed：引擎尚未完成
    assert task["metadata"]["agent"] == "faq-agent"
    assert task["metadata"]["submitted"] is True

    # 引擎里真有任务：type=agent.invoke，payload 携带 agent/input/_tenant_id（租户透传）
    with SessionLocal() as db:
        rec = db.get(TaskRecord, task["id"])
    assert rec is not None and rec.type == "agent.invoke"
    assert rec.payload["agent"] == "faq-agent"
    assert rec.payload["input"] == "异步问我"
    assert rec.payload["_tenant_id"] == dev_tenant

    # 任务最终完成，且引擎映射可经 tasks/get 回查到 completed
    snap = _poll_a2a_state(client, task["id"], {"completed"})
    assert snap["status"]["state"] == "completed"
    assert snap["metadata"]["agent"] == "faq-agent"


def test_async_message_send_unknown_agent_rejected(client: TestClient):
    """async_task=true + 未知 agent：提交前拦截（-32001），不产生引擎任务。"""
    from sqlalchemy import func

    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(TaskRecord)
                           .where(TaskRecord.type == "agent.invoke"))
    resp = client.post("/a2a/rpc", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                   "metadata": {"agent": "no-such-m45", "async_task": True}}})
    assert resp.json()["error"]["code"] == -32001
    with SessionLocal() as db:
        after = db.scalar(select(func.count()).select_from(TaskRecord)
                          .where(TaskRecord.type == "agent.invoke"))
    assert after == before  # 未提交任何引擎任务


# ---------- ② 终态挂点：COMPLETED → 签名回执（artifacts 含 mock 输出） ----------

def test_async_message_send_pushes_receipt_on_completion(client: TestClient, push_capture):
    cfg = {"url": "http://callback.test/async-hook", "token": "tok-m45",
           "authentication": {}}
    task = _async_send(client, 1, text="如何创建知识库？", cfg=cfg)
    assert task["status"]["state"] == "working"

    # 推送配置已按引擎 task_id 预登记（可经 pushNotificationConfig/get 回查）
    resp = client.post(RPC, headers=AUTH, json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/pushNotificationConfig/get",
        "params": {"taskId": task["id"]}})
    assert resp.json()["result"]["pushNotificationConfig"]["url"] == cfg["url"]

    # 轮询直到 completed（tasks/get 走引擎任务映射）
    snap = _poll_a2a_state(client, task["id"], {"completed"})
    assert snap["status"]["state"] == "completed"

    # 回执：URL、taskId、completed 状态、HMAC-SHA256 签名（key=推送配置 token）
    push = _wait_push(push_capture, 1)
    assert len(push) == 1
    assert push[0]["url"] == "http://callback.test/async-hook"
    payload = json.loads(push[0]["body"])
    assert payload["kind"] == "task-push" and payload["taskId"] == task["id"]
    assert payload["status"]["state"] == "completed"
    text = "".join(p["text"] for p in payload["task"]["artifacts"][0]["parts"])
    assert "mock-llm" in text  # artifacts 含 mock 输出
    expected = hmac.new(b"tok-m45", push[0]["body"], hashlib.sha256).hexdigest()
    assert push[0]["headers"]["X-EAP-Signature"] == f"sha256={expected}"
    assert push[0]["headers"]["X-EAP-Task-Id"] == task["id"]


# ---------- ③ 终态挂点：FAILED → failed 回执（含 error 信息） ----------

def test_async_message_send_pushes_failed_receipt(client: TestClient, push_capture,
                                                  monkeypatch):
    from eap.agents.registry import registry

    async def boom(db, name, request, trace_id=None):
        raise RuntimeError("boom-m45-a2a")

    monkeypatch.setattr(registry, "invoke", boom)  # 引擎 handler 内 invoke 失败 → FAILED
    cfg = {"url": "http://callback.test/async-fail", "token": "tok-fail"}
    task = _async_send(client, 1, text="会失败", cfg=cfg)

    snap = _poll_a2a_state(client, task["id"], {"failed"})
    assert snap["status"]["state"] == "failed"
    assert "boom-m45-a2a" in snap["status"]["message"]["parts"][0]["text"]

    push = _wait_push(push_capture, 1)
    assert len(push) == 1
    payload = json.loads(push[0]["body"])
    assert payload["status"]["state"] == "failed"
    assert "boom-m45-a2a" in payload["task"]["status"]["message"]["parts"][0]["text"]
    assert "artifacts" not in payload["task"]  # failed 无产物


# ---------- ④ tasks/get 引擎任务状态映射 ----------

def test_tasks_get_maps_engine_task_states(client: TestClient):
    dev_tenant = _dev_tenant_id()
    rows = {  # task_id → (引擎态, 期望 A2A 态)
        "m45-map-pending": ("PENDING", "working"),
        "m45-map-running": ("RUNNING", "working"),
        "m45-map-waiting": ("WAITING_HUMAN", "working"),
        "m45-map-completed": ("COMPLETED", "completed"),
        "m45-map-failed": ("FAILED", "failed"),
        "m45-map-cancelled": ("CANCELLED", "canceled"),
    }
    try:
        with SessionLocal() as db:
            for tid, (state, _a2a) in rows.items():
                result = ({"output": "异步产物文本"} if state == "COMPLETED"
                          else {"error": "异步失败原因"} if state == "FAILED" else {})
                db.add(TaskRecord(id=tid, type="agent.invoke", state=state,
                                  payload={"agent": "faq-agent", "input": "x",
                                           "_tenant_id": dev_tenant},
                                  result=result))
            db.commit()

        for tid, (state, a2a_state) in rows.items():
            resp = client.post(RPC, headers=AUTH, json={
                "jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": tid}})
            body = resp.json()
            assert "result" in body, (tid, body)
            task = body["result"]
            assert task["status"]["state"] == a2a_state, (tid, task)
            assert task["metadata"]["agent"] == "faq-agent"
        # completed：artifacts 从 result.output 构造
        resp = client.post(RPC, headers=AUTH, json={
            "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
            "params": {"id": "m45-map-completed"}})
        task = resp.json()["result"]
        assert task["artifacts"][0]["parts"][0]["text"] == "异步产物文本"
        # failed：status.message 携带 error（tasks/get 兼容）
        resp = client.post(RPC, headers=AUTH, json={
            "jsonrpc": "2.0", "id": 3, "method": "tasks/get",
            "params": {"id": "m45-map-failed"}})
        task = resp.json()["result"]
        assert "异步失败原因" in task["status"]["message"]["parts"][0]["text"]
    finally:  # 清理：PENDING 行防后续引擎启动 _recover_pending 误捞执行
        with SessionLocal() as db:
            for tid in rows:
                row = db.get(TaskRecord, tid)
                if row is not None:
                    db.delete(row)
            db.commit()


def test_tasks_get_engine_task_tenant_isolation(client: TestClient):
    """payload._tenant_id 不匹配（含 /api/v1/tasks 直提的无 _tenant_id 任务）→
    视为不存在（不泄漏存在性）；属主租户可查。"""
    from eap.models import ApiKey, Tenant
    from eap.security_keys import key_hash

    suffix = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        other = Tenant(name=f"m45-iso-{suffix}")
        db.add(other)
        db.flush()
        db.add(ApiKey(key=f"k-m45-iso-{suffix}",
                      key_hash=key_hash(f"k-m45-iso-{suffix}"), tenant_id=other.id))
        db.add(TaskRecord(id=f"m45-iso-{suffix}", type="agent.invoke", state="COMPLETED",
                          payload={"agent": "faq-agent", "_tenant_id": other.id},
                          result={"output": "别人的结果"}))
        db.add(TaskRecord(id=f"m45-notenant-{suffix}", type="agent.invoke",
                          state="COMPLETED", payload={"agent": "faq-agent"},
                          result={"output": "x"}))
        db.commit()

    # 跨租户 + 无 _tenant_id：dev 凭证查询一律 -32001
    for tid in (f"m45-iso-{suffix}", f"m45-notenant-{suffix}"):
        resp = client.post(RPC, headers=AUTH, json={
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": tid}})
        assert resp.json()["error"]["code"] == -32001, tid

    # 属主租户：映射正常
    resp = client.post("/a2a/rpc?agent=faq-agent",
                       headers={"Authorization": f"Bearer k-m45-iso-{suffix}"},
                       json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                             "params": {"id": f"m45-iso-{suffix}"}})
    assert resp.json()["result"]["status"]["state"] == "completed"


# ---------- ⑤ 无推送配置：不投递 ----------

def test_async_message_send_without_push_config_delivers_nothing(
        client: TestClient, push_capture):
    task = _async_send(client, 1)  # 不携带 pushNotificationConfig
    assert task["status"]["state"] == "working"
    _poll_a2a_state(client, task["id"], {"completed"})
    time.sleep(0.3)  # 推送窗口：无配置不应有任何 A2A 出站
    assert push_capture == []


def test_a2a_delegate_allowlist_kind_creatable_via_api(client: TestClient):
    """M46-C 缺口回归：a2a-delegate-allowlist 须经 policies API 可创建（此前 kind
    正则/_KINDS 漏登记致 422，存量测试绕过 API 直插库未暴露）；config 校验同步生效。"""
    from .conftest import AUTH

    r = client.post("/api/v1/policies", headers=AUTH, json={
        "name": "m46-a2a-gate", "kind": "a2a-delegate-allowlist",
        "config": {"endpoints": ["https://a2a.partner.example.com"], "agents": ["ext-agent"]}})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "a2a-delegate-allowlist"

    # config 校验：endpoints 与 agents 均缺失 → 400
    r = client.post("/api/v1/policies", headers=AUTH, json={
        "name": "m46-a2a-gate-bad", "kind": "a2a-delegate-allowlist", "config": {}})
    assert r.status_code == 400 and "endpoints" in r.json()["detail"]

    # 清理：停用防污染共享测试库（fail-closed 运行时语义不变）
    client.post("/api/v1/policies/m46-a2a-gate/enabled?enabled=false", headers=AUTH)

"""A2A 深化测试（M30）：message/stream SSE、平台级 discovery、A2A Client（注入 transport）、
a2a.delegate 工具边界/审计、推送配置与 HMAC 回执、跨租户任务隔离。全部离线确定性。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.runtime.a2a_client import (
    A2AClient,
    A2AProtocolError,
    A2ATransportError,
    TransportResponse,
    a2a_delegate_tool,
)

from .conftest import AUTH


@pytest.fixture(scope="module", autouse=True)
def _cleanup_a2a_policies(client: TestClient):
    """M52-D 模块级差量清理（模式同 test_tool_governance）：本模块直插库的
    a2a-delegate-allowlist 策略测后删除——策略按 kind 检索（非按名），残留的启用行
    会让后续「默认拒绝」语义断言（fail-closed）被上一遍/其他文件污染。"""
    from eap.db import SessionLocal
    from eap.models import PolicyRecord

    with SessionLocal() as db:
        before = set(db.scalars(select(PolicyRecord.id)).all())
    yield
    with SessionLocal() as db:
        for r in db.scalars(select(PolicyRecord)).all():
            if r.id not in before:
                db.delete(r)
        db.commit()


_EXT_CARD_URL = "http://ext.test/.well-known/agent-card.json"
_EXT_RPC_URL = "http://ext.test/rpc"
_EXT_CARD = {
    "name": "ext-agent", "description": "外部翻译 Agent", "version": "1.0.0",
    "protocolVersion": "1.0", "url": _EXT_RPC_URL, "preferredTransport": "JSONRPC",
    "capabilities": {"streaming": True, "pushNotifications": False},
}


def _ext_task(rpc_id, text="translated: 你好") -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id,
            "result": {"id": "task-ext-1", "contextId": "ctx-1",
                       "status": {"state": "completed"},
                       "artifacts": [{"name": "response",
                                      "parts": [{"kind": "text", "text": text}]}],
                       "metadata": {"agent": "ext-agent"}}}


class FakeA2AServer:
    """离线外部 A2A Server：实现 A2A Client 的 transport 协议（request + stream_events）。"""

    def __init__(self, *, fail: bool = False, rpc_error: dict | None = None):
        self.requests: list[dict] = []
        self.fail = fail  # True → 模拟 HTTP 500
        self.rpc_error = rpc_error  # 非 None → 回 JSON-RPC error 帧

    async def request(self, method, url, *, headers=None, content=None, timeout=10.0):
        self.requests.append({"method": method, "url": url, "content": content})
        if self.fail:
            return TransportResponse(500, "boom")
        if method == "GET" and url == _EXT_CARD_URL:
            return TransportResponse(200, json.dumps(_EXT_CARD))
        if method == "POST" and url == _EXT_RPC_URL:
            body = json.loads(content)
            if self.rpc_error is not None:
                return TransportResponse(200, json.dumps({**self.rpc_error, "id": body["id"]}))
            return TransportResponse(200, json.dumps(_ext_task(body["id"])))
        return TransportResponse(404, "not found")

    async def stream_events(self, method, url, *, headers=None, content=None, timeout=10.0):
        body = json.loads(content)
        rid = body["id"]
        for text in ("你好", "，世界"):
            yield json.dumps({"jsonrpc": "2.0", "id": rid,
                              "result": {"kind": "artifact-update", "taskId": "task-ext-9",
                                         "artifact": {"name": "response",
                                                      "parts": [{"kind": "text", "text": text}]},
                                         "append": True, "final": False}})
        yield json.dumps({"jsonrpc": "2.0", "id": rid,
                          "result": {"kind": "status-update", "taskId": "task-ext-9",
                                     "status": {"state": "completed"}, "final": True}})


def _dev_tenant_id() -> int:
    from eap.db import SessionLocal
    from eap.models import Tenant

    with SessionLocal() as db:
        return db.scalar(select(Tenant).where(Tenant.name == "dev")).id


# ---------- ① message/stream：SSE 流式调用平台 agent ----------


def _sse_frames(lines: list[str]) -> list[dict]:
    frames = []
    for line in lines:
        line = line.strip()
        if line.startswith("data:"):
            frames.append(json.loads(line[5:]))
    return frames


def test_message_stream_sse(client: TestClient):
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/stream",
            "params": {"message": {"role": "user",
                                   "parts": [{"kind": "text", "text": "如何创建知识库？"}]}}}
    with client.stream("POST", "/a2a/rpc?agent=faq-agent", headers=AUTH, json=body) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = list(resp.iter_lines())
    frames = _sse_frames(lines)
    assert frames and all(f["jsonrpc"] == "2.0" for f in frames)
    results = [f["result"] for f in frames if "result" in f]

    # 首帧：working 状态
    assert results[0]["kind"] == "status-update"
    assert results[0]["status"]["state"] == "working" and results[0]["final"] is False
    # token 帧：artifact-update 增量文本（离线 mock 确定性回复）
    artifacts = [r for r in results if r.get("kind") == "artifact-update"]
    text = "".join(p["text"] for a in artifacts for p in a["artifact"]["parts"])
    assert "mock-llm" in text
    # 终止帧：completed + 完整 Task 快照
    finals = [r for r in results if r.get("kind") == "status-update" and r.get("final") is True]
    assert finals and finals[-1]["status"]["state"] == "completed"
    task = finals[-1]["task"]
    assert task["status"]["state"] == "completed" and task["metadata"]["citations"]
    assert "".join(p["text"] for p in task["artifacts"][0]["parts"]) == text

    # 终止帧任务可经 tasks/get 回查（同租户）
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}})
    assert resp.json()["result"]["id"] == task["id"]


def test_message_stream_unknown_agent(client: TestClient):
    resp = client.post("/a2a/rpc", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 3, "method": "message/stream",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                   "metadata": {"agent": "no-such"}}})
    assert resp.status_code == 200  # JSON-RPC 错误走 200 + error 字段（未开流即失败）
    assert resp.json()["error"]["code"] == -32001


# ---------- ③ 平台级 discovery：/.well-known/agents.json ----------


def test_platform_discovery_agents_json(client: TestClient):
    resp = client.get("/.well-known/agents.json")
    assert resp.status_code == 200
    cards = resp.json()
    names = {c["name"] for c in cards}
    assert "faq-agent" in names  # 启动引导后 ACTIVE（started）的智能体进入目录
    card = next(c for c in cards if c["name"] == "faq-agent")
    assert card["protocolVersion"] == "1.0"
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is True
    assert card["url"].endswith("/a2a/rpc?agent=faq-agent")
    assert card["provider"]["organization"] == "EAP"
    assert card["skills"]  # 字段完整：技能/知识库列表非空


def test_agent_card_capabilities_updated(client: TestClient):
    card = client.get("/.well-known/agent-card.json", params={"agent": "faq-agent"}).json()
    assert card["capabilities"] == {"streaming": True, "pushNotifications": True}


# ---------- ② A2A Client：注入 transport 的 discover / send / send_stream ----------


def test_a2a_client_discover_send_and_stream():
    server = FakeA2AServer()
    client = A2AClient("http://ext.test", transport=server)

    card = asyncio.run(client.discover())
    assert card["name"] == "ext-agent"
    assert server.requests[0] == {"method": "GET", "url": _EXT_CARD_URL, "content": None}

    # discover 后以 card.url 为 message/send 目标
    task = asyncio.run(client.send("你好"))
    assert server.requests[-1]["url"] == _EXT_RPC_URL
    sent = json.loads(server.requests[-1]["content"])
    assert sent["method"] == "message/send"
    assert sent["params"]["message"]["parts"][0]["text"] == "你好"
    assert A2AClient.task_state(task) == "completed"
    assert A2AClient.task_text(task) == "translated: 你好"

    # send_stream：SSE 逐帧解析为 result
    async def _collect():
        return [frame async for frame in client.send_stream("stream 一下")]

    frames = asyncio.run(_collect())
    deltas = ["".join(p["text"] for p in f["artifact"]["parts"])
              for f in frames if f.get("kind") == "artifact-update"]
    assert "".join(deltas) == "你好，世界"
    assert frames[-1]["kind"] == "status-update" and frames[-1]["final"] is True


def test_a2a_client_send_without_discover_uses_endpoint():
    """未 discover 时直接以 endpoint 作为 JSON-RPC 端点（允许 .../rpc 形态）。"""
    server = FakeA2AServer()
    client = A2AClient(_EXT_RPC_URL, transport=server)
    task = asyncio.run(client.send("hi"))
    assert server.requests[-1]["url"] == _EXT_RPC_URL
    assert A2AClient.task_text(task) == "translated: 你好"


def test_a2a_client_error_paths():
    server = FakeA2AServer(fail=True)
    client = A2AClient("http://ext.test", transport=server)
    try:
        asyncio.run(client.discover())
        raise AssertionError("应当抛出 A2ATransportError")
    except A2ATransportError:
        pass

    server = FakeA2AServer(rpc_error={"jsonrpc": "2.0", "error": {"code": -32001, "message": "无此 agent"}})
    client = A2AClient("http://ext.test", transport=server)
    asyncio.run(client.discover())
    try:
        asyncio.run(client.send("hi"))
        raise AssertionError("应当抛出 A2AProtocolError")
    except A2AProtocolError as e:
        assert "-32001" in str(e)

    class _BadTransport(FakeA2AServer):
        async def request(self, method, url, *, headers=None, content=None, timeout=10.0):
            return TransportResponse(200, "not-json")

    client = A2AClient("http://ext.test", transport=_BadTransport())
    try:
        asyncio.run(client.discover())
        raise AssertionError("应当抛出 A2AProtocolError")
    except A2AProtocolError:
        pass


def test_a2a_client_callable_transport_form():
    """transport 注入形态二：裸 async callable（request 协议）。"""

    async def fake(method, url, *, headers=None, content=None, timeout=10.0):
        return TransportResponse(200, json.dumps(_ext_task(1)))

    client = A2AClient(_EXT_RPC_URL, transport=fake)
    task = asyncio.run(client.send("hi"))
    assert A2AClient.task_state(task) == "completed"


# ---------- ② a2a.delegate 工具：默认拒绝 + 策略放行 + 审计 ----------


def test_a2a_delegate_tool_default_deny(client: TestClient):
    from eap.runtime import policy

    tool = a2a_delegate_tool(transport=FakeA2AServer())
    token = policy.set_tenant(_dev_tenant_id())
    try:
        out = asyncio.run(tool.handler(json.dumps({"endpoint": "http://ext.test", "message": "hi"})))
    finally:
        policy.reset_tenant(token)
    data = json.loads(out)
    assert "拒绝" in data["error"]  # fail-closed：无策略即拒绝，不触网


def test_a2a_delegate_tool_policy_allowed_and_audit(client: TestClient):
    from eap.db import SessionLocal
    from eap.models import AuditLog, PolicyRecord
    from eap.runtime import policy

    tenant_id = _dev_tenant_id()
    pname = f"e2e-a2a-allow-{uuid.uuid4().hex[:8]}"  # M52-D：策略名唯一化（脏库重跑不撞唯一约束）
    with SessionLocal() as db:
        db.add(PolicyRecord(name=pname, tenant_id=tenant_id,
                            kind="a2a-delegate-allowlist",
                            config={"endpoints": ["http://ext.test"], "agents": ["ext-agent"]}))
        db.commit()
    try:
        server = FakeA2AServer()
        tool = a2a_delegate_tool(transport=server)
        token = policy.set_tenant(tenant_id)
        try:
            out = asyncio.run(tool.handler(json.dumps({
                "endpoint": "http://ext.test", "target_agent": "ext-agent", "message": "你好"})))
        finally:
            policy.reset_tenant(token)
        data = json.loads(out)
        assert data["state"] == "completed"
        assert data["output"] == "translated: 你好"
        assert data["task_id"] == "task-ext-1"
        assert server.requests[-1]["url"] == _EXT_RPC_URL  # 经注入 transport 到达 mock 外端点

        # 审计：a2a.delegate 落库（目标 endpoint/agent/状态）
        with SessionLocal() as db:
            row = db.scalar(select(AuditLog).where(AuditLog.action == "a2a.delegate")
                            .order_by(AuditLog.id.desc()))
            assert row is not None
            assert row.target == "http://ext.test"
            assert row.detail["status"] == "ok"
            assert row.detail["agent"] == "ext-agent"
            assert row.detail["task_state"] == "completed"
    finally:
        with SessionLocal() as db:  # 清理策略，避免影响其他测试文件（模块级差量清理兜底）
            row = db.scalar(select(PolicyRecord).where(PolicyRecord.name == pname))
            if row is not None:
                db.delete(row)
                db.commit()


def test_a2a_delegate_tool_depth_guard(client: TestClient):
    """委派深度护栏复用 M24：递归达上限后拒绝。"""
    from eap.runtime import policy
    from eap.runtime.multi_agent import _depth

    tool = a2a_delegate_tool(transport=FakeA2AServer())
    token = policy.set_tenant(_dev_tenant_id())
    depth_token = _depth.set(3)  # 平台最大委派深度
    try:
        out = asyncio.run(tool.handler(json.dumps({"endpoint": "http://ext.test", "message": "hi"})))
    finally:
        _depth.reset(depth_token)
        policy.reset_tenant(token)
    assert "委派深度已达上限" in json.loads(out)["error"]


# ---------- ④ 跨租户边界：任务按凭证租户隔离 ----------


def test_tasks_tenant_isolation(client: TestClient):
    from eap.db import SessionLocal
    from eap.models import ApiKey, Tenant
    from eap.security_keys import key_hash

    suffix = uuid.uuid4().hex[:6]
    # M52-D：API Key 明文/哈希同样唯一化——api_keys.key 有唯一约束，固定
    # "k-a2a-iso" 在上一遍残留行（本测不删）时脏库重跑撞 UNIQUE
    iso_key = f"k-a2a-iso-{suffix}"
    with SessionLocal() as db:
        other = Tenant(name=f"a2a-iso-{suffix}")
        db.add(other)
        db.flush()
        db.add(ApiKey(key=iso_key, key_hash=key_hash(iso_key), tenant_id=other.id))
        db.commit()

    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}}})
    task_id = resp.json()["result"]["id"]

    resp = client.post("/a2a/rpc?agent=faq-agent",
                       headers={"Authorization": f"Bearer {iso_key}"},
                       json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task_id}})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32001  # 跨租户查询视为不存在


# ---------- ① 推送配置：接受/保存/回查 + HMAC 签名回执 ----------


def test_push_config_receipt_with_hmac(client: TestClient, monkeypatch):
    """message/send 携带 pushNotificationConfig → 任务完成后同步投递签名回执。"""
    import eap.api.v1.a2a as a2a_mod

    captured: dict = {}

    async def fake_post(url, content, headers):
        captured.update({"url": url, "body": content, "headers": headers})
        return 200

    monkeypatch.setattr(a2a_mod, "_push_post", fake_post)
    cfg = {"url": "http://callback.test/hook", "token": "shhh", "authentication": {}}
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                   "configuration": {"pushNotificationConfig": cfg}}})
    task = resp.json()["result"]

    # 回执：URL、taskId、completed 状态、HMAC-SHA256 签名（key=推送配置 token）
    assert captured["url"] == "http://callback.test/hook"
    body = json.loads(captured["body"])
    assert body["kind"] == "task-push" and body["taskId"] == task["id"]
    assert body["status"]["state"] == "completed"
    expected = hmac.new(b"shhh", captured["body"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-EAP-Signature"] == f"sha256={expected}"
    assert captured["headers"]["X-EAP-Task-Id"] == task["id"]

    # 配置已保存（租户隔离键），可经 tasks/pushNotificationConfig/get 回查
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/pushNotificationConfig/get",
        "params": {"taskId": task["id"]}})
    result = resp.json()["result"]
    assert result["taskId"] == task["id"]
    assert result["pushNotificationConfig"]["url"] == "http://callback.test/hook"


def test_push_config_set_get_roundtrip(client: TestClient):
    """tasks/pushNotificationConfig（set）：对已有任务登记推送配置并回查。"""
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}}})
    task_id = resp.json()["result"]["id"]

    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/pushNotificationConfig",
        "params": {"taskId": task_id,
                   "pushNotificationConfig": {"url": "http://callback.test/2", "token": "t2"}}})
    assert resp.json()["result"]["accepted"] is True

    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 3, "method": "tasks/pushNotificationConfig/get",
        "params": {"taskId": task_id}})
    assert resp.json()["result"]["pushNotificationConfig"]["token"] == "t2"

    # 未知任务 → -32001
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 4, "method": "tasks/pushNotificationConfig",
        "params": {"taskId": "nope",
                   "pushNotificationConfig": {"url": "http://callback.test/3"}}})
    assert resp.json()["error"]["code"] == -32001

    # 缺 url → -32602
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 5, "method": "tasks/pushNotificationConfig",
        "params": {"taskId": task_id, "pushNotificationConfig": {}}})
    assert resp.json()["error"]["code"] == -32602


def test_unknown_rpc_method(client: TestClient):
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {"id": "x"}})
    assert resp.json()["error"]["code"] == -32601

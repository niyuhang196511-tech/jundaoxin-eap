"""A2A 1.0 对外端点测试：Agent Card / message/send / tasks/get / 鉴权。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_well_known_agent_card(client: TestClient):
    resp = client.get("/.well-known/agent-card.json", params={"agent": "faq-agent"})
    assert resp.status_code == 200
    card = resp.json()
    assert card["protocolVersion"] == "1.0"
    assert card["name"] == "faq-agent"
    assert card["url"].endswith("/a2a/rpc?agent=faq-agent")
    assert any(s["id"] == "website-faq" for s in card["skills"])


def test_message_send_and_tasks_get(client: TestClient):
    # message/send → completed Task + 文本 artifact + 引用 metadata
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "如何创建知识库？"}]}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0" and "error" not in data
    task = data["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"]
    assert task["metadata"]["citations"]

    # tasks/get 回查同一任务
    resp = client.post("/a2a/rpc?agent=faq-agent", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}})
    assert resp.status_code == 200
    assert resp.json()["result"]["id"] == task["id"]


def test_message_send_unknown_agent(client: TestClient):
    resp = client.post("/a2a/rpc", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 3, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                   "metadata": {"agent": "no-such"}}})
    assert resp.status_code == 200  # JSON-RPC 错误走 200 + error 字段
    assert resp.json()["error"]["code"] == -32001


def test_rpc_requires_auth(client: TestClient):
    resp = client.post("/a2a/rpc", json={
        "jsonrpc": "2.0", "id": 4, "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}}})
    assert resp.status_code == 401

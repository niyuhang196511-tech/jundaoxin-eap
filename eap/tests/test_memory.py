"""记忆体系测试：会话历史 / 长期记忆召回 / 遗忘 / faq-agent 多轮。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_memory_write_recall_forget(client: TestClient):
    # 写入用户级长期记忆
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": "u-100", "kind": "preference",
        "content": "用户偏好简洁回答，不需要寒暄",
    })
    assert resp.status_code == 200
    memory_id = resp.json()["id"]

    # 召回：语义相关查询应命中
    resp = client.post("/api/v1/memory/recall", headers=AUTH,
                       json={"query": "用户喜欢什么样的回答风格", "user_id": "u-100"})
    hits = resp.json()["hits"]
    assert hits and "简洁" in hits[0]["content"]

    # 列表
    rows = client.get("/api/v1/memory", headers=AUTH, params={"user_id": "u-100"}).json()
    assert any(r["id"] == memory_id for r in rows)

    # 遗忘
    assert client.delete(f"/api/v1/memory/{memory_id}", headers=AUTH).status_code == 200
    hits = client.post("/api/v1/memory/recall", headers=AUTH,
                       json={"query": "回答风格", "user_id": "u-100"}).json()["hits"]
    assert hits == []


def test_memory_scope_validation(client: TestClient):
    resp = client.post("/api/v1/memory", headers=AUTH,
                       json={"scope": "user", "content": "缺 user_id"})
    assert resp.status_code == 400


def test_session_memory_multi_turn(client: TestClient):
    """同一 session_id 两次调用：faa-agent 记录会话历史，实现多轮。"""
    session_id = uuid.uuid4().hex
    r1 = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                     json={"input": "如何创建知识库？", "session_id": session_id})
    assert r1.status_code == 200
    r2 = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                     json={"input": "注册手写的呢？", "session_id": session_id})
    assert r2.status_code == 200

    # 会话历史已落库（两条 user + 两条 assistant）
    history = client.get("/api/v1/memory", headers=AUTH,
                         params={"session_id": session_id}).json()
    roles = [m["kind"] for m in history if m["kind"] == "message"]
    assert len(roles) == 4

    # history 接口（经 faq-agent 内部逻辑）应还原为正序对话
    from eap.db import SessionLocal
    from eap.runtime.memory import memory_service

    with SessionLocal() as db:
        msgs = memory_service.history(db, session_id, limit=6)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert "知识库" in msgs[0]["content"]
    assert "手写" in msgs[2]["content"]


def test_isolated_sessions(client: TestClient):
    s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
    for s in (s1, s2):
        client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                    json={"input": f"会话 {s[:4]} 的消息", "session_id": s})
    from eap.db import SessionLocal
    from eap.runtime.memory import memory_service

    with SessionLocal() as db:
        assert len(memory_service.history(db, s1, limit=10)) == 2
        assert len(memory_service.history(db, s2, limit=10)) == 2

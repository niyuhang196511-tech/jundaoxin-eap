"""OpenAI 兼容端点 + Agent Loop 测试。"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from .conftest import AUTH

TOOLS = [{
    "type": "function",
    "function": {
        "name": "kb.product-docs.search",
        "description": "检索产品文档",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}]


def test_chat_completions_mock(client: TestClient):
    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "auto",
        "messages": [{"role": "user", "content": "你好，介绍一下你自己"}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert "mock" in data["choices"][0]["message"]["content"]
    assert data["usage"]["total_tokens"] > 0
    assert resp.headers.get("x-trace-id")


def test_chat_stream_sse(client: TestClient):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "messages": [{"role": "user", "content": "流式测试"}],
        "stream": True,
    }) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(resp.iter_text())
    assert body.rstrip().endswith("data: [DONE]")
    assert "chat.completion.chunk" in body


def test_tool_call_roundtrip(client: TestClient):
    """OpenAI 标准两段式：第一段返回 tool_calls，客户端执行工具后回传 tool 结果。"""
    user = {"role": "user", "content": "下单流程是什么"}
    resp = client.post("/v1/chat/completions", headers=AUTH,
                       json={"messages": [user], "tools": TOOLS})
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tc = data["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "kb.product-docs.search"
    assert "query" in tc["function"]["arguments"]

    # 客户端执行工具，回传结果 → 模型汇总
    messages = [
        user,
        {"role": "assistant", "content": None, "tool_calls": data["choices"][0]["message"]["tool_calls"]},
        {"role": "tool", "tool_call_id": tc["id"],
         "content": json.dumps({"context": ["[1]（下单流程说明）库存确认后在 ERP 创建订单"],
                                "citations": []}, ensure_ascii=False)},
    ]
    resp = client.post("/v1/chat/completions", headers=AUTH,
                       json={"messages": messages, "tools": TOOLS})
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["finish_reason"] == "stop"
    assert "根据工具返回" in resp.json()["choices"][0]["message"]["content"]


def test_agent_loop_collects_citations():
    """直连 run_loop：mock 发起 kb 工具调用 → 循环器执行并收集 citations → 收敛。"""
    from eap.agents.runtime_tools import retriever_tool
    from eap.db import SessionLocal
    from eap.modelhub.router import hub
    from eap.runtime.loop import run_loop

    with SessionLocal() as db:
        result = asyncio.run(run_loop(
            hub, db,
            messages=[{"role": "user", "content": "下单流程是什么"}],
            system="测试系统提示",
            tools=[retriever_tool("product-docs")],
        ))
    assert result.citations, "emits_citations 工具的引用应被循环器收集"
    assert result.citations[0]["kb"] == "product-docs"
    assert any("工具调用" in s for s in result.steps)
    assert "根据工具返回" in result.content
    assert result.usage["tokens_in"] > 0


def test_chat_auth_required(client: TestClient):
    assert client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401

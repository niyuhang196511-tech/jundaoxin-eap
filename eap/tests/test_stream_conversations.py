"""M2 测试：token 级流式（chat SSE 帧数显著大于整段分片）+ 会话端点（列表/消息/删除）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_chat_stream_yields_many_token_frames(client: TestClient):
    """stream=true 时 SSE 帧应为词/短语级（真流式），而非 8 片整段分片。"""
    resp = client.post("/v1/chat/completions", headers=AUTH,
                       json={"model": "auto", "stream": True,
                             "messages": [{"role": "user", "content": "流式测试一句话"}]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in resp.text.split("\n") if line.startswith("data:")]
    # mock 输出 ~60 字：真流式按词/6字符切分 → 帧数应明显多于 8 片分片
    content_frames = [f for f in frames if '"finish_reason":null' in f or '"finish_reason": null' in f]
    assert len(content_frames) > 8, f"流式帧数不足（疑似仍为整段分片）: {len(content_frames)}"
    # [DONE] 终止帧
    assert frames[-1].strip() == "data: [DONE]"
    # 内容连续拼接 = 完整回复
    import json as _json

    joined = "".join(_json.loads(f[5:])["choices"][0]["delta"].get("content", "") for f in content_frames)
    assert "mock-llm" in joined and "流式测试一句话" in joined


def test_chat_stream_fallback_shape_with_tools(client: TestClient):
    """带工具的流式请求走整段分片回退，协议形状不变。"""
    tools = [{"type": "function", "function": {
        "name": "kb.website-faq.search", "description": "检索",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
    resp = client.post("/v1/chat/completions", headers=AUTH,
                       json={"model": "auto", "stream": True, "tools": tools,
                             "messages": [{"role": "user", "content": "查知识库"}]})
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    # 工具调用场景：mock 发起 tool_calls，content 帧为空但 delta 结构完整
    assert '"chat.completion.chunk"' in resp.text


def test_conversations_list_messages_delete(client: TestClient):
    """agent 调用落会话消息 → 列表/回放/删除全链路。"""
    session_id = "conv-test-001"
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "你好，请问知识库怎么建？", "session_id": session_id})
    assert resp.status_code == 200, resp.text

    # 会话列表包含该会话
    convs = client.get("/api/v1/conversations", headers=AUTH).json()
    conv = next((c for c in convs if c["session_id"] == session_id), None)
    assert conv is not None, convs
    assert conv["agent"] == "faq-agent"
    assert conv["messages"] >= 2  # user + assistant
    assert conv["last_message"]

    # 按 agent 过滤
    convs_faq = client.get("/api/v1/conversations", headers=AUTH,
                           params={"agent": "faq-agent"}).json()
    assert all(c["agent"] == "faq-agent" for c in convs_faq)

    # 消息回放：正序 + 角色标注
    msgs = client.get(f"/api/v1/conversations/{session_id}/messages", headers=AUTH).json()
    roles = [m["role"] for m in msgs]
    assert "user" in roles and "assistant" in roles
    assert roles[0] == "user"

    # 删除
    resp = client.delete(f"/api/v1/conversations/{session_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 2
    msgs_after = client.get(f"/api/v1/conversations/{session_id}/messages", headers=AUTH).json()
    assert msgs_after == []

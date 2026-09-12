"""MCP 集成测试：真实 Server（uvicorn 线程，仅绑定本机回环地址）+ 官方 SDK Client 端到端。

覆盖：tools/list、tools/call（kb_search）、平台作为 MCP Server 被 Claude/Cursor 等 Host 接入。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

# 固定回环地址 + 固定端口（全字面量；端口被占用则跳过本模块）
TEST_HOST = "127.0.0.1"
MCP_TEST_PORT = 8931


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((TEST_HOST, port))
            return True
        except OSError:
            return False


@pytest.fixture(scope="module")
def live_server():
    """独立线程跑真实 uvicorn（共享测试进程 settings/临时库）。"""
    if not _port_free(MCP_TEST_PORT):
        pytest.skip(f"端口 {MCP_TEST_PORT} 被占用，跳过 MCP 集成测试")
    from eap.main import create_app

    config = uvicorn.Config(create_app(), host=TEST_HOST, port=MCP_TEST_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get("http://127.0.0.1:8931/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("测试服务器未启动")
    yield
    server.should_exit = True
    thread.join(timeout=5)


def test_mcp_endpoint_raw_jsonrpc(live_server):
    """原始 JSON-RPC：tools/list（Streamable HTTP stateless）。"""
    r = httpx.post("http://127.0.0.1:8931/mcp",
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"}, timeout=10)
    assert r.status_code == 200
    text = r.text
    assert "kb_search" in text and "agent_chat" in text


def test_mcp_official_client_end_to_end(live_server):
    """官方 SDK Client：initialize → list_tools → call_tool(kb_search)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run():
        async with streamable_http_client("http://127.0.0.1:8931/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = [t.name for t in listing.tools]
                assert {"kb_search", "agent_chat", "list_capabilities"} <= set(names)
                result = await session.call_tool(
                    "kb_search", {"kb": "website-faq", "query": "如何创建知识库", "top_k": 2})
                text = "\n".join(getattr(c, "text", "") for c in result.content)
                return names, text

    names, text = asyncio.run(run())
    assert "kb_search" in names
    assert "citations" in text and "context" in text

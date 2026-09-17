"""MCP 集成测试：真实 Server（uvicorn 线程，仅绑定本机回环地址）+ 官方 SDK Client 端到端。

覆盖：tools/list、tools/call（kb_search）、平台作为 MCP Server 被 Claude/Cursor 等 Host 接入。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

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
    """独立进程跑真实 uvicorn（与 TestClient 的 app 实例完全隔离——同进程双实例的
    MCP session manager / lifespan 互相干扰，全量顺序下 health 起不来）。

    共享同一测试库（EAP_DB_URL 由 conftest 设定），数据面与 TestClient 用例互通。
    """
    import os
    import subprocess

    if not _port_free(MCP_TEST_PORT):
        # 端口被占：若是存活 eap 实例则复用（服务已就绪），否则跳过
        try:
            if httpx.get(f"http://127.0.0.1:{MCP_TEST_PORT}/health", timeout=1).status_code == 200:
                yield None
                return
        except Exception:
            pass
        pytest.skip(f"端口 {MCP_TEST_PORT} 被占用，跳过 MCP 集成测试")

    env = {**os.environ, "EAP_HOST": TEST_HOST, "EAP_PORT": str(MCP_TEST_PORT), "EAP_SKIP_MIGRATIONS": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "eap"], env=env,
                            stdout=None, stderr=None)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{MCP_TEST_PORT}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("测试服务器未启动")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_mcp_endpoint_raw_jsonrpc(live_server):
    """原始 JSON-RPC：tools/list（Streamable HTTP stateless）。需平台 API Key。"""
    r = httpx.post("http://127.0.0.1:8931/mcp",
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": "Bearer dev-key-1"}, timeout=10)
    assert r.status_code == 200
    text = r.text
    assert "kb_search" in text and "agent_chat" in text


def test_mcp_endpoint_requires_api_key(live_server):
    """安全收口：无 key / 错 key → 401（EAP-3001），不泄漏工具清单。"""
    base = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    r = httpx.post("http://127.0.0.1:8931/mcp", json=base, headers=accept, timeout=10)
    assert r.status_code == 401 and "EAP-3001" in r.text
    assert "kb_search" not in r.text
    r = httpx.post("http://127.0.0.1:8931/mcp", json=base,
                   headers={**accept, "Authorization": "Bearer wrong-key"}, timeout=10)
    assert r.status_code == 401


def test_mcp_auth_disabled_flag():
    """EAP_MCP_AUTH=0：内网可信环境关闭门禁（独立 app 验证构建期开关）。"""
    import os

    from eap.config import get_settings

    os.environ["EAP_MCP_AUTH"] = "0"
    get_settings.cache_clear()
    try:
        from eap.main import create_app
        from fastapi.testclient import TestClient

        # base_url 带端口：MCP 库的 Host 白名单是 127.0.0.1:*（裸 host 会被 421）
        with TestClient(create_app(), base_url="http://127.0.0.1:8300") as c:
            r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 200 and "kb_search" in r.text
    finally:
        os.environ.pop("EAP_MCP_AUTH", None)
        get_settings.cache_clear()


def test_mcp_official_client_end_to_end(live_server):
    """官方 SDK Client：initialize → list_tools → call_tool(kb_search)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run():
        import httpx as _httpx

        async with _httpx.AsyncClient(
                headers={"Authorization": "Bearer dev-key-1"}) as http:
            async with streamable_http_client(
                    "http://127.0.0.1:8931/mcp", http_client=http) as (read, write):
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

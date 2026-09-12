"""MCP Client：从外部 MCP Server 动态拉取工具并包装为平台 Tool（docs/04 §5）。

每次调用按需建连（配合 stateless server 零会话负担）；
长连接复用与重连退避在 M2 Task Engine 并发化时接入。
"""

from __future__ import annotations

import json

from .tools import Tool


async def load_mcp_tools(server_url: str, prefix: str = "mcp") -> list[Tool]:
    """连接外部 MCP Server，把 tools/list 的每个工具包装为平台 Tool。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def with_session(fn):
        async with streamable_http_client(server_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    def make_handler(tool_name: str):
        async def handler(arguments: str) -> str:
            try:
                args = json.loads(arguments) if arguments and arguments.strip() else {}
            except json.JSONDecodeError:
                args = {}

            async def call(session):
                result = await session.call_tool(tool_name, args)
                parts = []
                for c in result.content:
                    text = getattr(c, "text", None)
                    if text:
                        parts.append(text)
                return "\n".join(parts) or json.dumps({"structured": str(result)})

            return await with_session(call)
        return handler

    listing = await with_session(lambda s: s.list_tools())
    tools: list[Tool] = []
    for t in listing.tools:
        tools.append(Tool(
            name=f"{prefix}.{t.name}",
            description=t.description or f"MCP 工具 {t.name}",
            parameters=t.inputSchema or {"type": "object", "properties": {}},
            handler=make_handler(t.name),
        ))
    return tools

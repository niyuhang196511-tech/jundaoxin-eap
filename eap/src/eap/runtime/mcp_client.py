"""MCP Client：从外部 MCP Server 动态拉取工具并包装为平台 Tool（docs/04 §5）。

传输：http（streamable_http_client）| stdio（stdio_client 子进程，本地手写 server 调试）。
每次调用按需建连（配合 stateless server 零会话负担）；
长连接复用与重连退避在 M2 Task Engine 并发化时接入。
"""

from __future__ import annotations

import contextlib
import json

from .tools import Tool


async def load_mcp_tools(server_url: str, prefix: str = "mcp") -> list[Tool]:
    """连接外部 MCP Server（http），把 tools/list 的每个工具包装为平台 Tool。"""
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
            parameters=t.input_schema or {"type": "object", "properties": {}},
            handler=make_handler(t.name),
        ))
    return tools


async def load_mcp_tools_stdio(command: str, args: list[str] | None = None,
                               prefix: str = "mcp") -> list[Tool]:
    """连接本地手写 MCP Server（stdio 子进程），工具包装同 http。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args or [])

    @contextlib.asynccontextmanager
    async def _session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def make_handler(tool_name: str):
        async def handler(arguments: str) -> str:
            try:
                call_args = json.loads(arguments) if arguments and arguments.strip() else {}
            except json.JSONDecodeError:
                call_args = {}
            async with _session() as session:
                result = await session.call_tool(tool_name, call_args)
                parts = [getattr(c, "text", "") for c in result.content if getattr(c, "text", None)]
                return "\n".join(parts) or json.dumps({"structured": str(result)})
        return handler

    async with _session() as session:
        listing = await session.list_tools()
    tools: list[Tool] = []
    for t in listing.tools:
        tools.append(Tool(
            name=f"{prefix}.{t.name}",
            description=t.description or f"MCP 工具 {t.name}",
            parameters=t.input_schema or {"type": "object", "properties": {}},
            handler=make_handler(t.name),
        ))
    return tools

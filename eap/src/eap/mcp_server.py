"""MCP Server：把平台能力（知识库检索、智能体对话）暴露为 MCP 工具（docs/04 §5）。

- 端点：POST /mcp（Streamable HTTP，stateless 模式——每个请求独立，免会话管理）
- 客户端：Claude / Cursor / Harness 等任何 MCP Host 均可直接接入（需平台 API Key）
- 每个平台实例独立创建 MCPServer（会话管理器绑定各自事件循环，不可跨实例复用）
- 反向身份（平台作为 MCP Client 消费外部 Server）见 runtime/mcp_client.py
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """MCP 端点 API Key 门禁（docs/04 §5 安全收口，仅拦 /mcp 前缀）。

    Bearer 凭证复用平台 API Key 体系（与 REST 同源校验）；MCP 是服务间协议，
    不接受嵌入会话令牌。EAP_MCP_AUTH=0 可关闭（仅限内网可信部署）。
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not self._check_key(token):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "EAP-3001 MCP 端点需要有效的 API Key（Authorization: Bearer <api-key>）"},
                status_code=401,
                headers={"www-authenticate": "Bearer"},
            )
        return await call_next(request)

    @staticmethod
    def _check_key(token: str) -> bool:
        if not token:
            return False
        from sqlalchemy import select

        from .db import SessionLocal
        from .models import ApiKey

        with SessionLocal() as db:
            row = db.scalar(select(ApiKey).where(ApiKey.key == token,
                                                 ApiKey.enabled == True))  # noqa: E712
            return row is not None


def create_mcp_server() -> tuple[MCPServer, object]:
    """创建 MCPServer 及其 Starlette 应用（stateless + JSON 响应）。"""
    mcp = MCPServer("eap")

    @mcp.tool()
    def kb_search(kb: str, query: str, top_k: int = 3) -> str:
        """检索 EAP 企业知识库，返回带来源编号的资料 JSON（回答时标注 [n]）。"""
        from .db import SessionLocal
        from .knowledge import service as kb_svc
        from .models import KB

        with SessionLocal() as db:
            record = db.query(KB).filter_by(name=kb).first()
            if record is None:
                return json.dumps({"error": f"知识库 {kb} 不存在"}, ensure_ascii=False)
            hits = kb_svc.retrieve(db, record, query, top_k=top_k)
            return json.dumps({
                "context": [f"[{i}]（{h['citation']['document']}）{h['content']}"
                            for i, h in enumerate(hits, 1)],
                "citations": [h["citation"] for h in hits],
            }, ensure_ascii=False)

    @mcp.tool()
    async def agent_chat(agent: str, input: str) -> str:
        """调用平台注册的智能体完成一次对话（等价 /api/v1/agents/{agent}/invocations）。"""
        from .agents.registry import registry
        from .db import SessionLocal
        from .schemas import InvokeRequest

        with SessionLocal() as db:
            resp = await registry.invoke(db, agent, InvokeRequest(input=input))
            return json.dumps({
                "output": resp.output,
                "agent_version": resp.agent_version,
                "citations": [c.model_dump() for c in resp.citations],
            }, ensure_ascii=False)

    @mcp.tool()
    def list_capabilities() -> str:
        """列出平台当前可用的知识库与智能体目录。"""
        from sqlalchemy import select

        from .agents.registry import registry
        from .db import SessionLocal
        from .models import KB

        with SessionLocal() as db:
            kbs = [k.name for k in db.scalars(select(KB)).all()]
        return json.dumps({"knowledge_bases": kbs, "agents": registry.names()}, ensure_ascii=False)

    return mcp, mcp.streamable_http_app(stateless_http=True, json_response=True)
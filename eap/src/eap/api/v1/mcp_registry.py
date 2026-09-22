"""MCP Registry API：外部 MCP Server 纳管 / 验证 / 启停（docs/04 §5）。

transport=http：url 直连（streamable HTTP）；transport=stdio：本地手写 server 子进程
（扩展开发体系：mcp_client.load_mcp_tools_stdio 拉取工具清单）。
"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import MCPServerRecord
from ...runtime.mcp_client import load_mcp_tools, load_mcp_tools_stdio
from ...runtime.mcp_auth import build_mcp_headers, get_mcp_token
from ...security_crypto import encrypt_secret
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/mcp/servers",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class ServerCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    url: str = ""
    transport: str = Field(default="http", pattern=r"^(http|stdio)$")
    command: str = ""
    args: list[str] = Field(default_factory=list)
    header_name: str = "Authorization"
    api_key: str | None = None
    # OAuth 客户端凭证（M34/L8）：oauth_token_url 非空即启用（api_key 回退保留）
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str | None = None
    oauth_scopes: str = ""
    enabled: bool = True


@router.get("")
def list_servers(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": s.name, "url": s.url, "transport": s.transport, "command": s.command,
         "args": s.args, "status": s.status, "enabled": s.enabled,
         "tools": s.tools, "created_at": str(s.created_at)}
        for s in db.scalars(select(MCPServerRecord)).all()
    ]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def register_server(body: ServerCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if body.transport == "http" and not body.url:
        raise fastapi.HTTPException(status_code=422, detail="http 传输需要 url")
    if body.transport == "stdio" and not body.command:
        raise fastapi.HTTPException(status_code=422, detail="stdio 传输需要 command")
    if db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 MCP Server {body.name} 已注册")
    record = MCPServerRecord(
        name=body.name, url=body.url, transport=body.transport,
        command=body.command, args=body.args,
        header_name=body.header_name, api_key=body.api_key, enabled=body.enabled,
        oauth_token_url=body.oauth_token_url, oauth_client_id=body.oauth_client_id,
        oauth_client_secret_enc=encrypt_secret(body.oauth_client_secret) if body.oauth_client_secret else None,
        oauth_scopes=body.oauth_scopes,
    )
    db.add(record)
    db.commit()
    audit.record("mcp.register", actor=audit.actor_of(request), target=record.name,
                 detail={"transport": record.transport}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "transport": record.transport, "status": record.status}


@router.post("/{name}/validate")
async def validate_server(name: str, db: Session = fastapi.Depends(get_db)):
    """连接外部 Server → tools/list → 记录工具清单（验证可达性与能力）。"""
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")

    try:
        if record.transport == "stdio":
            tools = await load_mcp_tools_stdio(record.command, record.args, prefix=name)
        else:
            url = record.url.rstrip("/") + "/mcp" if "/mcp" not in record.url else record.url
            # 认证（M34/L8）：OAuth token（过期自动重取）优先，回退 api_key，再退无认证
            token = await get_mcp_token(db, record)
            headers = build_mcp_headers(record, token)
            tools = await load_mcp_tools(url, prefix=name, headers=headers or None)
        tool_infos = [{"name": t.name, "description": t.description, "parameters": t.parameters}
                      for t in tools]
        record.status = "verified"
        record.tools = [t["name"] for t in tool_infos]
        db.commit()
        return {"name": name, "status": "verified", "tools": tool_infos}
    except Exception as e:
        record.status = "unreachable"
        db.commit()
        return {"name": name, "status": "unreachable", "error": str(e)[:200]}


@router.post("/{name}/oauth/token", dependencies=[fastapi.Depends(require_admin)])
async def refresh_token(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """手动刷新 OAuth access token（admin + 审计；响应不回显明文 token）。"""
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")
    if not record.oauth_token_url:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-7004 MCP Server {name} 未配置 OAuth token_url")
    try:
        await get_mcp_token(db, record, force=True)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=502, detail=str(e))
    audit.record("mcp.oauth.refresh", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "status": "refreshed", "expires_at": str(record.oauth_expires_at)}


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_server(name: str, enabled: bool, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")
    record.enabled = enabled
    db.commit()
    audit.record("mcp.toggle", actor=audit.actor_of(request), target=name,
                 detail={"enabled": enabled}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "enabled": enabled}

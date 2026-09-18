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
from ...models import MCPServerRecord
from ...runtime.mcp_client import load_mcp_tools, load_mcp_tools_stdio
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
def register_server(body: ServerCreate, db: Session = fastapi.Depends(get_db)):
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
    )
    db.add(record)
    db.commit()
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
            tools = await load_mcp_tools(url, prefix=name)
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


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_server(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

"""MCP Registry API：外部 MCP Server 纳管 / 验证 / 启停（docs/04 §5）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import MCPServerRecord
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/mcp/servers",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class ServerCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    url: str = Field(min_length=1)
    header_name: str = "Authorization"
    api_key: str | None = None
    enabled: bool = True


@router.get("")
def list_servers(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": s.name, "url": s.url, "status": s.status, "enabled": s.enabled,
         "tools": s.tools, "created_at": str(s.created_at)}
        for s in db.scalars(select(MCPServerRecord)).all()
    ]


@router.post("")
def register_server(body: ServerCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 MCP Server {body.name} 已注册")
    record = MCPServerRecord(
        name=body.name, url=body.url, header_name=body.header_name,
        api_key=body.api_key, enabled=body.enabled,
    )
    db.add(record)
    db.commit()
    return {"name": record.name, "url": record.url, "status": record.status}


@router.post("/{name}/validate")
async def validate_server(name: str, db: Session = fastapi.Depends(get_db)):
    """连接外部 Server → tools/list → 记录工具清单（验证可达性与能力）。"""
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {"Accept": "application/json, text/event-stream"}
            if record.api_key:
                headers[record.header_name or "Authorization"] = record.api_key
            resp = await client.post(
                record.url.rstrip("/") + "/mcp" if "/mcp" not in record.url else record.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=headers,
            )
            resp.raise_for_status()
            text = resp.text
            tools = []
            if text.lstrip().startswith("event:") or "\ndata:" in text:
                for line in text.splitlines():
                    if line.startswith("data:"):
                        data = json_loads(line[5:].strip())
                        tools = [t["name"] for t in
                                 (data.get("result", {}).get("tools") or [])]
                        break
            else:
                data = json_loads(text)
                tools = [t["name"] for t in (data.get("result", {}).get("tools") or [])]
            record.status = "verified"
            record.tools = tools
            db.commit()
            return {"name": name, "status": "verified", "tools": tools}
    except Exception as e:
        record.status = "unreachable"
        db.commit()
        return {"name": name, "status": "unreachable", "error": str(e)[:200]}


def json_loads(text: str) -> dict:
    import json

    return json.loads(text)


@router.patch("/{name}")
def toggle_server(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(MCPServerRecord).where(MCPServerRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 MCP Server {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

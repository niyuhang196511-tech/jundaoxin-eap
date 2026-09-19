"""扩展中心 API（扩展开发体系）：工具清单/试运行、插件加载、RAG 组件清单。"""

from __future__ import annotations

import json

import fastapi
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...knowledge.components import list_components as list_rag_components
from ...plugins import loaded_plugins, load_plugins, reload_plugins
from ...runtime.workflow import _WORKFLOW_TOOLS, resolve_tool
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/extensions",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


# ---------- 工具（内置 + 手写 workflow tool + MCP 桥接） ----------

@router.get("/tools")
def list_tools(db: Session = fastapi.Depends(get_db)):
    """全部可用工具：工作流注册表 + 各已注册智能体声明的工具 + MCP Server 桥接。"""
    from ...models import MCPServerRecord

    tools: list[dict] = []
    for name, factory in sorted(_WORKFLOW_TOOLS.items()):
        try:
            tool = factory()
            tools.append({"name": tool.name, "description": tool.description,
                          "parameters": tool.parameters, "origin": "workflow",
                          "requires_approval": tool.requires_approval})
        except Exception:
            tools.append({"name": name, "description": "（实例化失败）", "parameters": {},
                          "origin": "workflow", "requires_approval": False})

    seen = {t["name"] for t in tools}
    for agent_name in registry.names():
        agent = registry.get(agent_name)
        instance = agent.instance
        toolset = getattr(instance, "tools", None) or []
        for tool in toolset:
            if tool.name not in seen:
                seen.add(tool.name)
                tools.append({"name": tool.name, "description": tool.description,
                              "parameters": tool.parameters, "origin": f"agent:{agent_name}",
                              "requires_approval": tool.requires_approval})


    for record in db.scalars(select(MCPServerRecord)).all():
        for tool_name in record.tools or []:
            display = f"mcp:{record.name}.{tool_name}"
            if display not in seen:
                seen.add(display)
                tools.append({"name": display, "description": f"MCP Server {record.name} 桥接工具（{record.transport}）",
                              "parameters": {}, "origin": f"mcp:{record.name}",
                              "requires_approval": False})
    return tools


class ToolInvoke(BaseModel):
    args: dict = {}


@router.post("/tools/{name:path}/invoke")
async def invoke_tool(name: str, body: ToolInvoke):
    """工具试运行（扩展中心调试面板）。"""
    try:
        tool = resolve_tool(name)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    try:
        out = await tool.handler(json.dumps(body.args, ensure_ascii=False))
    except Exception as e:
        raise fastapi.HTTPException(status_code=503, detail=f"EAP-4005 工具执行失败：{e}") from e
    return {"name": name, "output": out[:8000]}


# ---------- 插件 ----------

@router.get("/plugins")
def list_plugins():
    if not loaded_plugins():
        load_plugins()
    return [
        {"name": p.name, "kind": p.kind, "description": p.description, "status": p.status,
         "error": p.error, "exposes": p.exposes}
        for p in loaded_plugins()
    ]


@router.post("/plugins/reload", dependencies=[fastapi.Depends(require_admin)])
def reload_all_plugins():
    plugins = reload_plugins()
    failed = [p.name for p in plugins if p.status == "failed"]
    return {"loaded": len(plugins), "failed": failed}


# ---------- RAG 组件 ----------

@router.get("/rag-components")
def list_rag():
    return [{"name": c.name, "kind": c.kind, "description": c.description, "source": c.source}
            for c in list_rag_components()]

"""扩展中心 API（扩展开发体系）：工具清单/试运行、插件加载、RAG 组件清单。"""

from __future__ import annotations

import json

import fastapi
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...models import ExtensionRecord, MCPServerRecord
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

    tools: list[dict] = []
    for name, factory in sorted(_WORKFLOW_TOOLS.items()):
        try:
            tool = factory()
            tools.append({"name": tool.name, "description": tool.description,
                          "parameters": tool.parameters, "origin": "workflow",
                          "requires_approval": tool.requires_approval,
                          "runtime": getattr(tool, "runtime", "inproc")})
        except Exception:
            tools.append({"name": name, "description": "（实例化失败）", "parameters": {},
                          "origin": "workflow", "requires_approval": False,
                          "runtime": "inproc"})

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
                              "requires_approval": tool.requires_approval,
                              "runtime": getattr(tool, "runtime", "inproc")})


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


# ---------- 扩展目录（v0.7-⑨：统一注册表，按 type 分组） ----------

@router.get("/registry")
def extension_registry(type: str | None = None, db: Session = fastapi.Depends(get_db)):
    """持久扩展注册表：全部类型扩展的目录（name/version/state/exposes）。"""
    q = select(ExtensionRecord).order_by(ExtensionRecord.type, ExtensionRecord.name)
    if type:
        q = q.where(ExtensionRecord.type == type)
    return [
        {"name": r.name, "type": r.type, "version": r.version, "title": r.title,
         "description": r.description, "state": r.state, "source": r.source,
         "exposes": r.exposes or [], "error": r.error, "updated_at": str(r.updated_at)}
        for r in db.scalars(q).all()
    ]


@router.get("/registry/{name}/manifest")
def extension_manifest(name: str, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 扩展 {name} 不存在")
    return {"name": record.name, "type": record.type, "version": record.version,
            "manifest": record.manifest, "state": record.state}


@router.post("/registry/{name}/enable", dependencies=[fastapi.Depends(require_admin)])
def enable_extension(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """启用扩展：disabled → enabled 并热重载生效（register 执行）。"""
    from ...observability import audit

    record = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 扩展 {name} 不存在")
    if record.state == "failed":
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-4006 扩展 {name} 加载失败，无法启用")
    record.state = "enabled"
    db.commit()
    reload_plugins()
    audit.record("extension.enable", actor=audit.actor_of(request), target=name,
                 detail={"version": record.version}, trace_id=getattr(request.state, "trace_id", ""))
    updated = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
    return {"name": name, "state": updated.state, "exposes": updated.exposes or []}


@router.post("/registry/{name}/disable", dependencies=[fastapi.Depends(require_admin)])
def disable_extension(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """停用扩展：热重载后不执行 register（工具/组件从注册表消失），状态跨重启保持。"""
    from ...observability import audit

    record = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 扩展 {name} 不存在")
    record.state = "disabled"
    db.commit()
    reload_plugins()
    audit.record("extension.disable", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    updated = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
    return {"name": name, "state": updated.state}


# ---------- RAG 组件 ----------

@router.get("/rag-components")
def list_rag():
    return [{"name": c.name, "kind": c.kind, "description": c.description, "source": c.source}
            for c in list_rag_components()]


# ---------- Bundle 安装/卸载（v0.7：开发者接入生命周期） ----------

@router.post("/install", dependencies=[fastapi.Depends(require_admin)])
async def install_bundle(request: fastapi.Request):
    """安装 .eapext bundle（multipart 上传）：校验 → 解压 → 热加载 → 注册表登记 → 审计。"""
    from ...observability import audit
    from ...config import get_settings
    from ...runtime.bundles import install_bundle

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 缺少 file 字段（.eapext zip）")
    data = await upload.read()
    try:
        result = install_bundle(
            data, plugins_root=get_settings().plugins_dir,
            actor=audit.actor_of(request), trace_id=getattr(request.state, "trace_id", ""))
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return result


@router.delete("/registry/{name}", dependencies=[fastapi.Depends(require_admin)])
def uninstall_extension(name: str, request: fastapi.Request):
    """卸载扩展：移除插件目录 + 注册表记录（审计）。"""
    from ...observability import audit
    from ...config import get_settings
    from ...runtime.bundles import uninstall_bundle

    try:
        result = uninstall_bundle(
            name, plugins_root=get_settings().plugins_dir,
            actor=audit.actor_of(request), trace_id=getattr(request.state, "trace_id", ""))
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return result

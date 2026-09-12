"""A2A 1.0 对外端点（docs/04 §5）：外部 Agent 生态经 JSON-RPC 发现并调用平台智能体。

- GET  /.well-known/agent-card.json?agent=<name>   Agent Card（能力名片，公开）
- POST /a2a/rpc                                    JSON-RPC 2.0：message/send、tasks/get

语义映射：A2A Task（completed）→ 平台 Invocation；流式/推送通知暂不支持（M3）。
"""

from __future__ import annotations

import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...schemas import InvokeRequest
from ..deps import require_api_key, resolve_tenant

_A2A_TASKS: dict[str, dict] = {}


def _card_for(name: str, base_url: str) -> dict:
    agent = registry.get(name)
    return {
        "name": agent.manifest.name,
        "description": agent.manifest.description,
        "version": agent.manifest.version,
        "protocolVersion": "1.0",
        "url": f"{base_url}/a2a/rpc?agent={name}",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": kb, "name": kb, "description": f"知识库 {kb}"}
                   for kb in agent.manifest.knowledge]
        or [{"id": "chat", "name": "对话", "description": agent.manifest.description}],
        "provider": {"organization": "EAP", "url": base_url},
    }


# ---------- Agent Card（公开：名片按设计可被发现） ----------

wellknown = fastapi.APIRouter()


@wellknown.get("/.well-known/agent-card.json")
def agent_card_wellknown(request: fastapi.Request, agent: str = "faq-agent"):
    try:
        return _card_for(agent, str(request.base_url).rstrip("/"))
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e


# ---------- JSON-RPC 2.0（服务间调用，需 API Key） ----------

router = fastapi.APIRouter(dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class Part(BaseModel):
    kind: str = "text"
    text: str = ""


class A2AMessage(BaseModel):
    role: str = "user"
    parts: list[Part] = Field(default_factory=list)

    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.kind == "text")


class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict = Field(default_factory=dict)


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@router.post("/a2a/rpc")
async def a2a_rpc(
    body: JSONRPCRequest,
    request: fastapi.Request,
    agent: str | None = fastapi.Query(None),
    db: Session = fastapi.Depends(get_db),
):
    target = agent or body.params.get("metadata", {}).get("agent")

    if body.method == "message/send":
        if not target:
            return _rpc_error(body.id, -32602, "缺少 agent（查询参数或 params.metadata.agent）")
        msg = A2AMessage(**body.params.get("message", {}))
        try:
            resp = await registry.invoke(db, target, InvokeRequest(input=msg.text()))
        except KeyError as e:
            return _rpc_error(body.id, -32001, f"智能体不存在: {e}")
        except RuntimeError as e:
            return _rpc_error(body.id, -32002, str(e))
        task = {
            "id": uuid.uuid4().hex,
            "contextId": resp.trace_id,
            "status": {"state": "completed"},
            "artifacts": [{"name": "response", "parts": [{"kind": "text", "text": resp.output}]}],
            "metadata": {"agent": target, "citations": [c.model_dump() for c in resp.citations]},
        }
        _A2A_TASKS[task["id"]] = task
        return {"jsonrpc": "2.0", "id": body.id, "result": task}

    if body.method == "tasks/get":
        task_id = body.params.get("id")
        task = _A2A_TASKS.get(str(task_id))
        if task is None:
            return _rpc_error(body.id, -32001, f"任务 {task_id} 不存在")
        return {"jsonrpc": "2.0", "id": body.id, "result": task}

    return _rpc_error(body.id, -32601, f"未知方法 {body.method}")

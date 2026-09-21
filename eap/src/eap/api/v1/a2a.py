"""A2A 1.0 对外端点（docs/04 §5）：外部 Agent 生态经 JSON-RPC 发现并调用平台智能体。

- GET  /.well-known/agent-card.json?agent=<name>   Agent Card（能力名片，公开）
- GET  /.well-known/agents.json                    平台级 discovery：全部 ACTIVE 智能体卡片目录（M30）
- POST /a2a/rpc                                    JSON-RPC 2.0：message/send、message/stream（SSE）、
                                                   tasks/get、tasks/pushNotificationConfig[/set|/get]

语义映射：A2A Task（completed）→ 平台 Invocation。message/stream（M30）以 SSE 产出
status-update / artifact-update 帧（A2A 1.0 子集），终止帧附完整 Task 快照并入库
tasks/get 可查。

推送通知（M30）：tasks/pushNotificationConfig 接受并保存配置（进程内 dict，租户隔离）；
平台任务在 RPC 内同步完成，回执于任务完成后同步投递（HMAC-SHA256 签名，X-EAP-Signature）。
限制：异步任务引擎（runtime/tasks.py）的完成点未挂钩——任务引擎归独立任务组演进，
跨进程异步回执待其暴露干净挂点后接入；当前覆盖 RPC 内同步完成的回执。

跨租户边界：入口按 resolve_tenant 凭证租户校验（任务/推送配置按租户隔离）；
出站委派见 runtime/a2a_client.py（a2a.delegate 工具，fail-closed 策略边界）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

import fastapi
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...agents.registry import registry, RegisteredAgent
from ...config import get_settings
from ...db import get_db
from ...runtime import agent_config, policy
from ...schemas import InvokeRequest
from ..deps import require_api_key, resolve_tenant

logger = logging.getLogger("eap.a2a")

_A2A_TASKS: dict[str, dict] = {}
_A2A_TASK_TENANT: dict[str, int] = {}  # 任务 → 创建租户（tasks/get 按凭证租户校验）
_A2A_PUSH: dict[str, dict] = {}  # f"{tenant_id}:{task_id}" → 推送配置


def _card_for(name: str, base_url: str) -> dict:
    agent = registry.get(name)
    return {
        "name": agent.manifest.name,
        "description": agent.manifest.description,
        "version": agent.manifest.version,
        "protocolVersion": "1.0",
        "url": f"{base_url}/a2a/rpc?agent={name}",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": kb, "name": kb, "description": f"知识库 {kb}"}
                   for kb in agent.manifest.knowledge]
        or [{"id": "chat", "name": "对话", "description": agent.manifest.description}],
        "provider": {"organization": "EAP", "url": base_url},
    }


# ---------- Agent Card 与平台级 discovery（公开：名片按设计可被发现） ----------

wellknown = fastapi.APIRouter()


@wellknown.get("/.well-known/agent-card.json")
def agent_card_wellknown(request: fastapi.Request, agent: str = "faq-agent"):
    try:
        return _card_for(agent, str(request.base_url).rstrip("/"))
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e


@wellknown.get("/.well-known/agents.json")
def agents_discovery(request: fastapi.Request):
    """平台级 discovery（M30）：全部运行中（registry 内 ACTIVE）智能体的卡片数组。

    仅纳管 status=started（已启动且健康检查通过）的智能体；registered/stopped/
    unhealthy 不对外暴露。字段复用单卡片构造函数，与 agent-card.json 同构。
    """
    base = str(request.base_url).rstrip("/")
    cards: list[dict] = []
    for a in registry.all():
        if a.status != "started":
            continue
        try:
            cards.append(_card_for(a.manifest.name, base))
        except KeyError:  # 注册表与实例竞态时跳过单个
            continue
    return cards


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


def _push_key(tenant_id: int, task_id: str) -> str:
    return f"{tenant_id}:{task_id}"


def _store_task(task: dict, tenant_id: int) -> None:
    _A2A_TASKS[task["id"]] = task
    _A2A_TASK_TENANT[task["id"]] = tenant_id


def _task_for(tenant_id: int, task_id) -> dict | None:
    """按凭证租户取任务：跨租户查询一律视为不存在（不泄漏存在性）。"""
    task = _A2A_TASKS.get(str(task_id))
    if task is None or _A2A_TASK_TENANT.get(str(task_id)) != tenant_id:
        return None
    return task


def _push_cfg_from_params(params: dict) -> dict | None:
    """提取推送配置：params.configuration.pushNotificationConfig（A2A 1.0），
    兼容顶层 params.pushNotificationConfig；缺 url 视为未提供。"""
    raw = ((params.get("configuration") or {}).get("pushNotificationConfig")
           or params.get("pushNotificationConfig"))
    if isinstance(raw, dict) and raw.get("url"):
        return {"url": str(raw["url"]), "token": str(raw.get("token") or ""),
                "authentication": raw.get("authentication") or {}}
    return None


async def _push_post(url: str, content: bytes, headers: dict) -> int:
    """推送回执出站 POST（独立函数便于测试注入 fake，返回 HTTP 状态码）。"""
    import httpx

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        resp = await client.post(url, content=content, headers=headers)
        return resp.status_code


async def deliver_push(tenant_id: int, task_id: str, config: dict, task: dict) -> bool:
    """推送回执投递（M30）：HMAC-SHA256 签名 POST 任务状态到回调 url。

    签名头 X-EAP-Signature: sha256=HMAC(key, body)，key 取推送配置 token 优先，
    回退平台 secret_key / 内置开发密钥；失败仅告警不阻断 RPC 响应。
    """
    payload = {"kind": "task-push", "taskId": task_id,
               "status": task.get("status"), "task": task}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    secret = config.get("token") or get_settings().secret_key or "eap-a2a-push"
    sign = hmac.new(str(secret).encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json",
               "X-EAP-Signature": f"sha256={sign}", "X-EAP-Task-Id": task_id}
    try:
        status = await _push_post(config["url"], body, headers)
        return status < 400
    except Exception as e:
        logger.warning("A2A 推送回执失败 %s: %s", config["url"], e)
        return False


def _completed_task(task_id: str, trace_id: str, target: str, output: str,
                    citations: list, extra_meta: dict | None = None) -> dict:
    task = {
        "id": task_id,
        "contextId": trace_id,
        "status": {"state": "completed"},
        "artifacts": [{"name": "response", "parts": [{"kind": "text", "text": output}]}],
        "metadata": {"agent": target, "citations": citations, **(extra_meta or {})},
    }
    return task


@router.post("/a2a/rpc")
async def a2a_rpc(
    body: JSONRPCRequest,
    request: fastapi.Request,
    agent: str | None = fastapi.Query(None),
    db: Session = fastapi.Depends(get_db),
):
    target = agent or body.params.get("metadata", {}).get("agent")
    tenant_id = getattr(request.state, "tenant_id", 0)

    if body.method == "message/send":
        if not target:
            return _rpc_error(body.id, -32602, "缺少 agent（查询参数或 params.metadata.agent）")
        msg = A2AMessage(**body.params.get("message", {}))
        if not msg.text().strip():
            return _rpc_error(body.id, -32602, "message 缺少 text part")
        token = policy.set_tenant(tenant_id)
        try:
            resp = await registry.invoke(db, target, InvokeRequest(input=msg.text()))
        except KeyError as e:
            return _rpc_error(body.id, -32001, f"智能体不存在: {e}")
        except RuntimeError as e:
            return _rpc_error(body.id, -32002, str(e))
        finally:
            policy.reset_tenant(token)
        task = _completed_task(uuid.uuid4().hex, resp.trace_id, target, resp.output,
                               [c.model_dump() for c in resp.citations])
        _store_task(task, tenant_id)
        cfg = _push_cfg_from_params(body.params)
        if cfg is not None:
            _A2A_PUSH[_push_key(tenant_id, task["id"])] = cfg
            # 同步场景回执：任务在 RPC 内完成，响应前投递（异步任务引擎完成点未挂钩，见模块 docstring）
            await deliver_push(tenant_id, task["id"], cfg, task)
        return {"jsonrpc": "2.0", "id": body.id, "result": task}

    if body.method == "message/stream":
        if not target:
            return _rpc_error(body.id, -32602, "缺少 agent（查询参数或 params.metadata.agent）")
        msg = A2AMessage(**body.params.get("message", {}))
        if not msg.text().strip():
            return _rpc_error(body.id, -32602, "message 缺少 text part")
        try:
            registered = registry.get(target)
        except KeyError as e:
            return _rpc_error(body.id, -32001, f"智能体不存在: {e}")
        trace_id = getattr(request.state, "trace_id", None) or uuid.uuid4().hex
        return StreamingResponse(
            _a2a_stream(registered, target, msg, body.id, trace_id, tenant_id, db,
                        push_cfg=_push_cfg_from_params(body.params)),
            media_type="text/event-stream",
        )

    if body.method in ("tasks/pushNotificationConfig", "tasks/pushNotificationConfig/set"):
        task_id = str(body.params.get("taskId") or body.params.get("id") or "")
        cfg = _push_cfg_from_params(body.params)
        if not task_id or cfg is None:
            return _rpc_error(body.id, -32602, "缺少 taskId 或 pushNotificationConfig.url")
        if _task_for(tenant_id, task_id) is None:
            return _rpc_error(body.id, -32001, f"任务 {task_id} 不存在")
        _A2A_PUSH[_push_key(tenant_id, task_id)] = cfg
        return {"jsonrpc": "2.0", "id": body.id,
                "result": {"taskId": task_id, "pushNotificationConfig": cfg, "accepted": True}}

    if body.method == "tasks/pushNotificationConfig/get":
        task_id = str(body.params.get("taskId") or body.params.get("id") or "")
        cfg = _A2A_PUSH.get(_push_key(tenant_id, task_id))
        if cfg is None:
            return _rpc_error(body.id, -32001, f"任务 {task_id} 无推送配置")
        return {"jsonrpc": "2.0", "id": body.id,
                "result": {"taskId": task_id, "pushNotificationConfig": cfg}}

    if body.method == "tasks/get":
        task_id = body.params.get("id")
        task = _task_for(tenant_id, task_id)
        if task is None:
            return _rpc_error(body.id, -32001, f"任务 {task_id} 不存在")
        return {"jsonrpc": "2.0", "id": body.id, "result": task}

    return _rpc_error(body.id, -32601, f"未知方法 {body.method}")


async def _a2a_stream(registered: RegisteredAgent, name: str, msg: A2AMessage, rpc_id,
                      trace_id: str, tenant_id: int, db: Session,
                      push_cfg: dict | None = None):
    """message/stream SSE 帧：status-update / artifact-update / 终止帧（A2A 1.0 子集）。

    帧格式（data: JSON）：{"jsonrpc","id","result"}，result.kind 区分事件；
    终止 status-update（final=True）额外携带 task 完整快照（平台约定，便于
    客户端免 tasks/get 回查）。
    """

    def frame(result: dict) -> str:
        return "data: " + json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result},
                                     ensure_ascii=False) + "\n\n"

    task_id = uuid.uuid4().hex
    agent = registered.instance
    yield frame({"kind": "status-update", "taskId": task_id, "contextId": trace_id,
                 "status": {"state": "working"}, "final": False})
    token = policy.set_tenant(tenant_id)
    pieces: list[str] = []
    citations: list = []
    extra_meta: dict = {}
    try:
        request = InvokeRequest(input=msg.text())
        if agent is None:
            raise RuntimeError(f"智能体 {name} 未启动，请先 start")
        # 配置版本覆盖层（v0.5）：流式路径与 registry.invoke 同语义
        config_version, overlay_cfg = agent_config.resolve_effective_overlay(db, name, request.output_schema)
        with agent_config.apply_overlay(overlay_cfg):
            async for event, data in agent.on_invoke_stream(request):
                if event == "token":
                    text = str(data.get("content") or "")
                    pieces.append(text)
                    yield frame({"kind": "artifact-update", "taskId": task_id, "contextId": trace_id,
                                 "artifact": {"name": "response",
                                              "parts": [{"kind": "text", "text": text}]},
                                 "append": True, "final": False})
                elif event == "step":
                    yield frame({"kind": "status-update", "taskId": task_id, "contextId": trace_id,
                                 "status": {"state": "working",
                                            "message": {"role": "agent",
                                                        "parts": [{"kind": "text",
                                                                   "text": str(data.get("step") or "")}]}},
                                 "final": False})
                elif event == "result":
                    citations = [c for c in (data.get("citations") or [])]
                    if data.get("interaction"):
                        extra_meta["interaction"] = data["interaction"]
        if config_version:
            extra_meta["config_version"] = config_version
        task = _completed_task(task_id, trace_id, name, "".join(pieces), citations, extra_meta)
        _store_task(task, tenant_id)
        if push_cfg is not None:
            _A2A_PUSH[_push_key(tenant_id, task_id)] = push_cfg
            await deliver_push(tenant_id, task_id, push_cfg, task)
        yield frame({"kind": "status-update", "taskId": task_id, "contextId": trace_id,
                     "status": {"state": "completed"}, "final": True, "task": task})
    except Exception as e:
        failed = {"id": task_id, "contextId": trace_id, "status": {"state": "failed",
                                                                  "message": {"role": "agent",
                                                                              "parts": [{"kind": "text", "text": str(e)}]}}}
        _store_task(failed, tenant_id)
        yield frame({"kind": "status-update", "taskId": task_id, "contextId": trace_id,
                     "status": failed["status"], "final": True, "task": failed})
    finally:
        policy.reset_tenant(token)

"""Agent API：目录 / 调用（同步+SSE）/ Agent Card / 配置版本层（v0.5）。"""

from __future__ import annotations

import json

import fastapi
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...models import AgentRecord, AgentVersionRecord, InteractionRecord
from ...observability import audit
from ...observability.middleware import record_usage
from ...runtime import agent_config, budget, policy
from ...schemas import InvokeRequest
from ..deps import require_admin, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/agents", dependencies=[fastapi.Depends(resolve_tenant)])


@router.get("")
def list_agents(db: Session = fastapi.Depends(get_db)):
    """Agent Registry 目录：三类纳管的 M1 视图（builtin/sdk/entrypoint）+ 配置发布指针。"""
    pointers = {r.name: r.published_version
                for r in db.scalars(select(AgentRecord)).all()
                if r.published_version}
    return [
        {
            "name": a.manifest.name,
            "version": a.manifest.version,
            "description": a.manifest.description,
            "source": a.source,
            "status": a.status,
            "health": a.health,
            "knowledge": a.manifest.knowledge,
            "embeddable": a.manifest.embeddable,
            "published_version": pointers.get(a.manifest.name),
        }
        for a in registry.all()
    ]


@router.post("/reload")
async def reload_agents():
    """热加载：重导入配置模块（同模块重注册 = 替换）→ 全部重新启动。"""
    count = await registry.reload_modules()
    return {"reloaded_modules": count, "agents": registry.names()}


@router.get("/{name}/card")
def agent_card(name: str):
    """A2A 风格 Agent Card（正式 A2A 1.0 端点在 M3，docs/04 §5）。"""
    a = registry.get(name)
    return {
        "name": a.manifest.name,
        "version": a.manifest.version,
        "description": a.manifest.description,
        "capabilities": {"streaming": True, "pushNotifications": False},
        "skills": [{"id": s, "description": f"知识库 {s}"} for s in a.manifest.knowledge],
        "provider": {"organization": "EAP", "url": ""},
        "endpoint": f"/api/v1/agents/{name}/invocations",
    }


@router.post("/{name}/stop")
async def stop_agent(name: str):
    """STOP：调用 on_stop 钩子；停用后调用返回 503。"""
    try:
        await registry.stop_agent(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    return {"name": name, "status": "stopped"}


@router.post("/{name}/start")
async def start_agent(name: str):
    """START：重新实例化 + 健康检查（stop 后恢复 / unhealthy 重拉）。"""
    try:
        registry.get(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    await registry.start_agent(name)
    agent = registry.get(name)
    return {"name": name, "status": agent.status, "health": agent.health}


@router.delete("/{name}")
async def unregister_agent(name: str):
    """注销：移出注册表并删除纳管记录（热加载 reload 可从源码恢复）。"""
    try:
        await registry.unregister(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    return {"name": name, "status": "unregistered"}


@router.post("/{name}/invocations")
async def invoke(
    name: str,
    body: InvokeRequest,
    request: fastapi.Request,
    db: Session = fastapi.Depends(get_db),
):
    # 嵌入会话令牌最小权限：只能调用绑定的那个智能体（docs/04 §6）
    embed_agent = getattr(request.state, "embed_agent", None)
    if embed_agent and embed_agent != name:
        raise fastapi.HTTPException(status_code=403, detail=f"EAP-3002 会话令牌仅限智能体 {embed_agent}")
    # 限流（v0.6-⑤）：按凭证每分钟上限
    from ...api.security import get_limiter

    credential = getattr(request.state, "auth_kind", "") + ":" + str(
        getattr(request.state, "user", "") or getattr(request.state, "tenant_id", 0))
    limiter = get_limiter("chat")
    if not await limiter.allow_async(credential):
        raise fastapi.HTTPException(status_code=429, detail="EAP-2001 请求过于频繁",
                                    headers={"Retry-After": str(limiter.retry_after(credential))})
    # 成本中心熔断：token 预算超限 → 429（docs/08 §4）
    try:
        budget.guard(db, getattr(request.state, "tenant_id", 0))
    except RuntimeError as e:
        raise fastapi.HTTPException(status_code=429, detail=str(e)) from e
    if body.stream:
        return StreamingResponse(
            _stream_invoke(name, body, request, db),
            media_type="text/event-stream",
        )
    import time as _time

    t0 = _time.monotonic()
    token = policy.set_tenant(getattr(request.state, "tenant_id", None))
    try:
        resp = await registry.invoke(db, name, body, trace_id=getattr(request.state, "trace_id", None))
    except policy.PolicyDenied as e:
        raise fastapi.HTTPException(status_code=403, detail=str(e)) from e
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except RuntimeError as e:
        raise fastapi.HTTPException(status_code=503, detail=f"EAP-4005 {e}") from e
    finally:
        policy.reset_tenant(token)
    record_usage(
        getattr(request.state, "trace_id", ""), getattr(request.state, "tenant_id", 0),
        kind="agent", model=resp.usage.get("model", name),
        tokens_in=resp.usage.get("tokens_in", 0), tokens_out=resp.usage.get("tokens_out", 0),
        latency_ms=int((_time.monotonic() - t0) * 1000),
    )
    return resp


async def _stream_invoke(name: str, body: InvokeRequest, request: fastapi.Request, db):
    """SSE 事件流：token 打字机 + 步骤事件 + 最终结果（Invocation 级）。"""
    import time as _time
    import uuid as _uuid

    t0 = _time.monotonic()
    trace_id = getattr(request.state, "trace_id", _uuid.uuid4().hex)

    def send(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield send("start", {"agent": name, "trace_id": trace_id})
    token = policy.set_tenant(getattr(request.state, "tenant_id", None))
    tokens_out = 0
    try:
        registered = registry.get(name)  # 未注册 → KeyError → 404
        agent = registered.instance
        if agent is not None and hasattr(agent, "on_invoke_stream"):
            # 配置版本覆盖层（v0.5）：流式路径与 registry.invoke 同语义
            config_version, overlay_cfg = agent_config.resolve_effective_overlay(db, name, body.output_schema)
            with agent_config.apply_overlay(overlay_cfg):
                try:
                    async for event, data in agent.on_invoke_stream(body):
                        if event == "token":
                            tokens_out += len(data.get("content", ""))
                        if event == "result" and config_version:
                            data = {**data, "config_version": config_version}
                        yield send(event, data)
                except Exception as e:
                    from ...runtime.interaction import InteractionRequested
                    from ...models import InteractionRecord

                    if isinstance(e, InteractionRequested):
                        # 交互引擎（v0.5）：流式挂起 → result 帧携带 interaction
                        payload = e.request
                        record = InteractionRecord(
                            id="itx-" + _uuid.uuid4().hex[:12],
                            agent=name, session_id=body.session_id,
                            schema=await payload.aresolved_schema(db, agent_name=name),
                            values={}, state="waiting", trace_id=trace_id,
                        )
                        db.add(record)
                        db.commit()
                        result_data = {
                            "content": payload.title or "等待用户输入",
                            "citations": [], "steps": [], "usage": {"model": "interaction"},
                            "config_version": config_version,
                            "interaction": {"id": record.id, "key": payload.key,
                                            "title": payload.title,
                                            "description": payload.description,
                                            "ui_schema": record.schema},
                        }
                        yield send("result", result_data)
                    else:
                        raise
        else:
            resp = await registry.invoke(db, name, body, trace_id=trace_id)
            for step in resp.steps:
                yield send("step", {"step": step})
            yield send("result", resp.model_dump())
            tokens_out = resp.usage.get("tokens_out", 0)
        record_usage(trace_id, getattr(request.state, "tenant_id", 0), kind="agent", model=name,
                     tokens_in=0, tokens_out=max(1, tokens_out // 4),
                     latency_ms=int((_time.monotonic() - t0) * 1000))
    except Exception as e:
        yield send("error", {"message": str(e)})
    finally:
        policy.reset_tenant(token)


# ---------- 配置版本层（v0.5-①：draft→publish→archived，语义同 Prompt 流水线） ----------

class VersionCreate(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    config: dict = Field(default_factory=dict)
    notes: str = Field(default="", max_length=256)


class VersionPatch(BaseModel):
    config: dict | None = None
    notes: str | None = Field(default=None, max_length=256)


def _require_agent(name: str):
    try:
        return registry.get(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e


@router.get("/{name}/versions")
def list_versions(name: str, db: Session = fastapi.Depends(get_db)):
    """版本列表 + 当前发布指针。"""
    _require_agent(name)
    pointer = db.scalar(select(AgentRecord).where(AgentRecord.name == name))
    rows = db.scalars(select(AgentVersionRecord).where(AgentVersionRecord.agent_name == name)
                      .order_by(AgentVersionRecord.created_at.desc())).all()
    return {
        "agent": name,
        "published_version": pointer.published_version if pointer else None,
        "versions": [
            {"version": r.version, "state": r.state, "notes": r.notes,
             "config": r.config, "created_at": str(r.created_at)} for r in rows
        ],
    }


@router.get("/{name}/versions/{version}")
def get_version(name: str, version: str, db: Session = fastapi.Depends(get_db)):
    _require_agent(name)
    row = db.scalar(select(AgentVersionRecord)
                    .where(AgentVersionRecord.agent_name == name,
                           AgentVersionRecord.version == version))
    if row is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 Agent {name}@{version} 配置版本不存在")
    return {"agent": name, "version": row.version, "state": row.state,
            "config": row.config, "notes": row.notes, "created_at": str(row.created_at)}


@router.post("/{name}/versions", dependencies=[fastapi.Depends(require_admin)])
def create_version(name: str, body: VersionCreate, request: fastapi.Request,
                   db: Session = fastapi.Depends(get_db)):
    _require_agent(name)
    try:
        row = agent_config.create_version(db, name, body.version, body.config, body.notes)
        db.commit()
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409 if "已存在" in str(e) else 400, detail=str(e)) from e
    audit.record("agent.version.create", actor=audit.actor_of(request), target=f"{name}@{body.version}",
                 detail={"notes": body.notes}, trace_id=getattr(request.state, "trace_id", ""))
    return {"agent": name, "version": row.version, "state": row.state}


@router.patch("/{name}/versions/{version}", dependencies=[fastapi.Depends(require_admin)])
def patch_version(name: str, version: str, body: VersionPatch, db: Session = fastapi.Depends(get_db)):
    _require_agent(name)
    try:
        row = agent_config.update_draft(db, name, version, body.config, body.notes)
        db.commit()
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    return {"agent": name, "version": row.version, "state": row.state}


@router.post("/{name}/versions/{version}/publish", dependencies=[fastapi.Depends(require_admin)])
def publish_version(name: str, version: str, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    _require_agent(name)
    try:
        row = agent_config.publish_version(db, name, version)
        db.commit()
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    audit.record("agent.version.publish", actor=audit.actor_of(request), target=f"{name}@{version}",
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"agent": name, "version": row.version, "state": row.state,
            "published_version": version}


@router.post("/{name}/versions/{version}/deprecate", dependencies=[fastapi.Depends(require_admin)])
def deprecate_version(name: str, version: str, request: fastapi.Request,
                      db: Session = fastapi.Depends(get_db)):
    _require_agent(name)
    try:
        row = agent_config.deprecate_version(db, name, version)
        db.commit()
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    audit.record("agent.version.deprecate", actor=audit.actor_of(request), target=f"{name}@{version}",
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"agent": name, "version": row.version, "state": row.state}


@router.post("/{name}/rollback", dependencies=[fastapi.Depends(require_admin)])
def rollback_versions(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """回滚：重发布最近一个 archived/deprecated 版本。"""
    _require_agent(name)
    try:
        row = agent_config.rollback_version(db, name)
        db.commit()
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    if row is None:
        raise fastapi.HTTPException(status_code=409, detail="EAP-6002 无可回滚的归档版本")
    audit.record("agent.version.rollback", actor=audit.actor_of(request),
                 target=f"{name}@{row.version}", trace_id=getattr(request.state, "trace_id", ""))
    return {"agent": name, "version": row.version, "state": row.state,
            "published_version": row.version}


@router.get("/{name}/versions/{version_a}/diff/{version_b}")
def diff_versions(name: str, version_a: str, version_b: str, db: Session = fastapi.Depends(get_db)):
    """两个配置版本的逐键差异（仅报告不同键）。"""
    _require_agent(name)
    rows = {r.version: r for r in db.scalars(select(AgentVersionRecord)
            .where(AgentVersionRecord.agent_name == name,
                   AgentVersionRecord.version.in_([version_a, version_b]))).all()}
    for v in (version_a, version_b):
        if v not in rows:
            raise fastapi.HTTPException(status_code=404,
                                        detail=f"EAP-4004 Agent {name}@{v} 配置版本不存在")
    cfg_a, cfg_b = rows[version_a].config or {}, rows[version_b].config or {}
    changes = []
    for key in sorted(set(cfg_a) | set(cfg_b)):
        if cfg_a.get(key) != cfg_b.get(key):
            changes.append({"key": key, "from": cfg_a.get(key), "to": cfg_b.get(key)})
    return {"agent": name, "from": version_a, "to": version_b, "changes": changes}


# ---------- 交互引擎（v0.5-④：聊天通道挂起/恢复 + 动态选项数据源） ----------

class InteractionValues(BaseModel):
    values: dict = Field(min_length=1, description="用户提交的表单值 {field_id: value}")
    session_id: str | None = None


class OptionRequest(BaseModel):
    field: str
    values: dict = Field(default_factory=dict, description="已填父字段值（级联取选项）")


def _require_waiting_interaction(db: Session, interaction_id: str, name: str) -> InteractionRecord:
    record = db.get(InteractionRecord, interaction_id)
    if record is None or record.agent != name:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 交互 {interaction_id} 不存在")
    if record.state != "waiting":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-4006 交互 {interaction_id} 状态为 {record.state}")
    return record


@router.post("/{name}/interactions/{interaction_id}/submit")
async def submit_interaction(name: str, interaction_id: str, body: InteractionValues,
                             request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """提交交互表单值 → 标记 submitted → 以交互恢复协议再次流式调用智能体（SSE）。

    恢复协议：input 为 JSON {"__interaction__": <id>, <field>: <value>}，
    智能体经 ctx.interaction_values(request) 读取提交值并从交互点继续。
    """
    embed_agent = getattr(request.state, "embed_agent", None)
    if embed_agent and embed_agent != name:
        raise fastapi.HTTPException(status_code=403, detail=f"EAP-3002 会话令牌仅限智能体 {embed_agent}")
    _require_agent(name)
    record = _require_waiting_interaction(db, interaction_id, name)
    record.values = body.values
    record.state = "submitted"
    db.commit()
    from ...observability import audit

    audit.record("interaction.submit", actor=audit.actor_of(request),
                 target=f"{name}:{interaction_id}", detail={"fields": sorted(body.values)},
                 trace_id=getattr(request.state, "trace_id", ""))
    resume_input = json.dumps({"__interaction__": interaction_id, **body.values}, ensure_ascii=False)
    resume_request = InvokeRequest(input=resume_input, stream=True,
                                   session_id=body.session_id or record.session_id)
    return StreamingResponse(
        _stream_invoke(name, resume_request, request, db),
        media_type="text/event-stream",
    )


@router.post("/{name}/interactions/{interaction_id}/options")
async def interaction_options(name: str, interaction_id: str, body: OptionRequest,
                              db: Session = fastapi.Depends(get_db)):
    """级联动态选项：按已填父字段值实时重取某字段的 options（调已注册 Tool + 审计）。"""
    _require_agent(name)
    record = db.get(InteractionRecord, interaction_id)
    if record is None or record.agent != name:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 交互 {interaction_id} 不存在")
    fields = {f.get("id"): f for f in (record.schema or {}).get("fields", [])}
    field = fields.get(body.field)
    if field is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 字段 {body.field} 不存在")
    data_source = field.get("data_source")
    if not data_source:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 字段 {body.field} 无动态数据源")
    from ...runtime.interaction import aresolve_options

    try:
        options = await aresolve_options(db, data_source, values=body.values, agent_name=name)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return {"interaction_id": interaction_id, "field": body.field, "options": options}

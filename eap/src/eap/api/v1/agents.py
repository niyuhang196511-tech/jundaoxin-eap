"""Agent API：目录 / 调用（同步+SSE）/ Agent Card。"""

from __future__ import annotations

import json

import fastapi
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...observability.middleware import record_usage
from ...runtime import budget, policy
from ...runtime import budget
from ...schemas import InvokeRequest
from ..deps import resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/agents", dependencies=[fastapi.Depends(resolve_tenant)])


@router.get("")
def list_agents():
    """Agent Registry 目录：三类纳管的 M1 视图（builtin/sdk/entrypoint）。"""
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
        }
        for a in registry.all()
    ]


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
    """SSE 事件流：步骤事件 + 最终结果（Invocation 级）。"""
    import time as _time
    import uuid as _uuid

    t0 = _time.monotonic()
    trace_id = getattr(request.state, "trace_id", _uuid.uuid4().hex)

    def send(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield send("start", {"agent": name, "trace_id": trace_id})
    token = policy.set_tenant(getattr(request.state, "tenant_id", None))
    try:
        resp = await registry.invoke(db, name, body, trace_id=trace_id)
        for step in resp.steps:
            yield send("step", {"step": step})
        yield send("result", resp.model_dump())
        record_usage(trace_id, getattr(request.state, "tenant_id", 0), kind="agent", model=name,
                     tokens_in=resp.usage.get("tokens_in", 0), tokens_out=resp.usage.get("tokens_out", 0),
                     latency_ms=int((_time.monotonic() - t0) * 1000))
    except Exception as e:
        yield send("error", {"message": str(e)})
    finally:
        policy.reset_tenant(token)

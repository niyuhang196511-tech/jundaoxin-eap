"""OpenAI 兼容端点：/v1/chat/completions（同步 JSON + SSE 流式）。"""

from __future__ import annotations

import json
import time
import uuid

import fastapi
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...db import get_db
from ...modelhub.providers import ProviderError
from ...modelhub.router import hub
from ...observability.middleware import record_usage
from ...runtime import budget, policy
from ...schemas import ChatCompletionRequest
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


def _now_ts() -> int:
    return int(time.time())


@router.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: fastapi.Request,
    db: Session = fastapi.Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)
    tenant_id = getattr(request.state, "tenant_id", 0)
    # 限流（v0.6-⑤）：按凭证每分钟上限（EAP_CHAT_RATE_LIMIT），429 带 Retry-After
    from ...api.security import get_limiter

    credential = getattr(request.state, "auth_kind", "") + ":" + str(
        getattr(request.state, "user", "") or getattr(request.state, "tenant_id", 0))
    limiter = get_limiter("chat")
    if not await limiter.allow_async(credential):
        raise fastapi.HTTPException(status_code=429, detail="EAP-2001 请求过于频繁",
                                    headers={"Retry-After": str(limiter.retry_after(credential))})
    # 成本中心熔断：token 预算超限 → 429（docs/08 §4）
    try:
        budget.guard(db, tenant_id)
    except RuntimeError as e:
        raise fastapi.HTTPException(status_code=429, detail=str(e)) from e
    messages = [m.model_dump(exclude_none=True) for m in body.messages]
    prefer = None if body.model in ("auto", "") else body.model
    t0 = time.monotonic()

    token = policy.set_tenant(tenant_id)
    try:
        completion = await hub.complete(
            db, messages, capability="chat", tools=body.tools or None,
            prefer=prefer, temperature=body.temperature,
        )
    except policy.PolicyDenied as e:
        raise fastapi.HTTPException(status_code=403, detail=str(e)) from e
    except ProviderError as e:
        raise fastapi.HTTPException(status_code=502, detail=f"EAP-4001 {e}") from e
    finally:
        policy.reset_tenant(token)

    result, record = completion.result, completion.record
    latency_ms = int((time.monotonic() - t0) * 1000)
    record_usage(trace_id, tenant_id, kind="chat", model=record.name,
                 tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=latency_ms)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not body.stream:
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": _now_ts(),
            "model": record.name,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.content,
                    **({"tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in result.tool_calls
                    ]} if result.tool_calls else {}),
                },
                "finish_reason": "tool_calls" if result.tool_calls else "stop",
            }],
            "usage": {
                "prompt_tokens": result.tokens_in,
                "completion_tokens": result.tokens_out,
                "total_tokens": result.tokens_in + result.tokens_out,
            },
            "x_trace_id": trace_id,
        }

    # SSE：真实 token 级流式（providers.stream_complete）；带工具时走整段再分片
    if body.tools:
        async def sse_fallback():
            content = result.content or ""
            piece = max(1, len(content) // 8)
            for i in range(0, max(len(content), 1), piece):
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk", "created": _now_ts(),
                    "model": record.name,
                    "choices": [{"index": 0, "delta": {"content": content[i:i + piece]}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            done = {
                "id": completion_id, "object": "chat.completion.chunk", "created": _now_ts(),
                "model": record.name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

        return StreamingResponse(sse_fallback(), media_type="text/event-stream")

    async def sse():
        emitted = 0
        try:
            async for text in hub.stream(db, messages, prefer=prefer, temperature=body.temperature):
                emitted += len(text)
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk", "created": _now_ts(),
                    "model": record.name,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except policy.PolicyDenied as e:
            err = {"error": {"message": str(e)}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            return
        except ProviderError as e:
            err = {"error": {"message": f"EAP-4001 {e}"}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            return
        done = {
            "id": completion_id, "object": "chat.completion.chunk", "created": _now_ts(),
            "model": record.name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        if emitted:
            # 计量修复（v0.6）：流式对话按真实租户/模型记录（此前 hub.stream 记在 tenant 0）
            record_usage(trace_id, tenant_id, kind="chat", model=record.name,
                         tokens_in=0, tokens_out=max(1, emitted // 4),
                         latency_ms=0)

    return StreamingResponse(sse(), media_type="text/event-stream")

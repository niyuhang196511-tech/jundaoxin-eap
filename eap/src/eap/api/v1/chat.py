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
from ...runtime import budget
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
    # 成本中心熔断：token 预算超限 → 429（docs/08 §4）
    try:
        budget.guard(db, tenant_id)
    except RuntimeError as e:
        raise fastapi.HTTPException(status_code=429, detail=str(e)) from e
    messages = [m.model_dump(exclude_none=True) for m in body.messages]
    prefer = None if body.model in ("auto", "") else body.model
    t0 = time.monotonic()

    try:
        completion = await hub.complete(
            db, messages, capability="chat", tools=body.tools or None,
            prefer=prefer, temperature=body.temperature,
        )
    except ProviderError as e:
        raise fastapi.HTTPException(status_code=502, detail=f"EAP-4001 {e}") from e

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

    # SSE：M1 以整段结果分片下发（线上协议正确，真实 token 级流式 M2 接入）
    async def sse():
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

    return StreamingResponse(sse(), media_type="text/event-stream")

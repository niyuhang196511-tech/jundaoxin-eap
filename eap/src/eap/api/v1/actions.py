"""Actions API（docs/unfinished v0.5-⑥）：Agent 输出卡片上的业务动作执行端点。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability.middleware import record_usage
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/actions",
                           dependencies=[fastapi.Depends(resolve_tenant),
                                         fastapi.Depends(require_api_key)])


class ActionInvoke(BaseModel):
    action: str = Field(min_length=1, description="动作标识（前端展示/审计用）")
    tool: str = Field(min_length=1, description="执行动作的工具名（kb/工作流/连接器/插件）")
    args: dict = Field(default_factory=dict)
    session_id: str | None = None
    agent: str | None = None


@router.post("/invoke")
async def invoke_action(body: ActionInvoke, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    """执行 UI 动作：工具统一解析（resolve_tool）→ 执行或转审批 → 审计 + 会话记忆。"""
    from ...runtime.actions import run_action

    try:
        result = await run_action(
            db, action=body.action, tool=body.tool, args=body.args,
            session_id=body.session_id, agent=body.agent,
            actor=_actor(request), trace_id=getattr(request.state, "trace_id", ""),
        )
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return result


def _actor(request: fastapi.Request) -> str:
    from ...observability.audit import actor_of

    return actor_of(request)

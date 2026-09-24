"""UI Action 运行时（docs/unfinished v0.5-⑥）：Agent 输出卡片上的业务动作按钮。

Action = {label, action, tool, args, confirmation}：
- 工具经 workflow.resolve_tool 统一解析（kb/工作流/连接器/插件）
- requires_approval 工具 → 转 agent.hitl 任务走既有审批流（WAITING_HUMAN）
- 调用写审计（action.invoke）；结果同时写会话记忆（tool 角色消息，agent 后续可见）
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..observability.audit import record as audit_record


async def run_action(db: Session, *, action: str, tool: str, args: dict,
                     session_id: str | None = None, agent: str | None = None,
                     actor: str = "system", trace_id: str = "") -> dict:
    """执行 UI 动作：解析工具 → 执行（或转审批）→ 审计 + 会话记忆。

    返回 {status: "executed"|"approval_required", ...}。
    """
    from ..observability.metrics import incr
    from .tools import Tool
    from .workflow import resolve_tool

    resolved: Tool | None
    try:
        resolved = resolve_tool(tool)
    except ValueError:
        resolved = None
    if resolved is None:
        raise ValueError(f"工具 {tool} 未注册，无法执行动作 {action}")

    audit_record("action.invoke", actor=actor, target=f"{action}:{tool}",
                 detail={"args": args, "session_id": session_id}, trace_id=trace_id)
    incr("eap_action_invocations_total", {"tool": tool})

    if resolved.requires_approval:
        # 高风险动作：转 agent.hitl 任务走既有审批流（WAITING_HUMAN）
        from ..db import SessionLocal
        from ..models import TaskRecord

        import uuid as _uuid

        task_id = "task-" + _uuid.uuid4().hex[:12]
        with SessionLocal() as tdb:
            tdb.add(TaskRecord(
                id=task_id, type="agent.hitl",
                payload={"agent": agent or "", "input": f"UI 动作 {action}（工具 {tool}）",
                         "_pending_tool": tool, "_pending_args": json.dumps(args, ensure_ascii=False)},
            ))
            tdb.commit()
        # 直接落 WAITING_HUMAN 快照（不经执行器：动作没有 agent 消息历史可恢复）
        with SessionLocal() as tdb:
            task = tdb.get(TaskRecord, task_id)
            task.state = "WAITING_HUMAN"
            task.result = {
                "pending": {"tool": tool, "arguments": json.dumps(args, ensure_ascii=False)},
                "approvals": {}, "source": "ui_action",
            }
            tdb.commit()
        return {"status": "approval_required", "action": action, "tool": tool,
                "task_id": task_id,
                "message": f"动作 {action} 为高风险操作，已转入人工审批（任务 {task_id}）"}

    out = await resolved.handler(json.dumps(args, ensure_ascii=False))
    _remember_action_result(db, session_id=session_id, agent=agent,
                            action=action, tool=tool, out=out)
    return {"status": "executed", "action": action, "tool": tool, "result": out[:8000]}


def _remember_action_result(db: Session, *, session_id: str | None, agent: str | None,
                            action: str, tool: str, out: str) -> None:
    """动作结果写会话记忆（tool 角色消息），agent 后续对话可见。"""
    if not session_id:
        return
    try:
        from ..models import MemoryRecord

        content = f"[动作 {action} · 工具 {tool}] {out[:2000]}"
        db.add(MemoryRecord(scope="session", kind="message", session_id=session_id,
                            agent=agent or "", content=content,
                            meta={"role": "tool", "action": action}))
        db.commit()
    except Exception:
        db.rollback()  # 记忆写入失败不影响动作结果返回

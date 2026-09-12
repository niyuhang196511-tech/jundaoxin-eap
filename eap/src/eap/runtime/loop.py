"""Agent Loop：Think → Tool Call →（RAG）→ 回答。

带步数预算、引用收集、工具级 HITL 审批门控与断点恢复（docs/03 §1、§5）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..modelhub.router import ModelHub
from .tools import Tool, find_tool


class TaskSuspended(Exception):
    """工具等待人工审批 → 任务挂起（携带可恢复的消息快照）。

    resume_messages 即挂起时的完整消息历史，审批决定注入后原样续跑。
    """

    def __init__(self, messages: list[dict], pending_tool: str, pending_args: str):
        self.messages = messages
        self.pending_tool = pending_tool
        self.pending_args = pending_args
        super().__init__(f"等待人工审批: {pending_tool}")


@dataclass
class RunResult:
    content: str
    steps: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    model: str = ""


async def run_loop(
    hub: ModelHub,
    db,
    *,
    messages: list[dict],
    system: str,
    tools: list[Tool],
    capability: str = "chat",
    prefer: str | None = None,
    max_steps: int = 4,
    approval_gate=None,
    resume_messages: list[dict] | None = None,
) -> RunResult:
    """执行 Agent 循环。

    approval_gate(tool_name) -> True（执行）/ False（否决，注入拒绝结果）/ None（挂起等待人工）。
    resume_messages：HITL 恢复时传入挂起快照的完整消息历史（替代 system+messages 重建）。
    """
    usage = {"tokens_in": 0, "tokens_out": 0}
    steps: list[str] = []
    citations: list[dict] = []
    if resume_messages:
        msgs = [dict(m) for m in resume_messages]
    else:
        msgs = [{"role": "system", "content": system}, *messages]
    tool_schemas = [t.schema() for t in tools] or None
    last_model = ""

    for step in range(1, max_steps + 1):
        completion = await hub.complete(
            db, msgs, capability=capability, tools=tool_schemas, prefer=prefer,
        )
        result, record = completion.result, completion.record
        last_model = record.name
        usage["tokens_in"] += result.tokens_in
        usage["tokens_out"] += result.tokens_out

        if result.tool_calls:
            steps.append(f"step{step}({record.name}): 发起工具调用 " + ",".join(tc.name for tc in result.tool_calls))
            msgs.append({
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in result.tool_calls
                ],
            })
            for tc in result.tool_calls:
                tool = find_tool(tools, tc.name)

                decision = True
                if tool is not None and tool.requires_approval and approval_gate is not None:
                    decision = approval_gate(tool.name)
                    if decision is None:
                        # HITL 挂起：快照包含未执行的 assistant tool_calls 消息
                        raise TaskSuspended(messages=[dict(m) for m in msgs], pending_tool=tc.name,
                                            pending_args=tc.arguments)

                if tool is None:
                    out = json.dumps({"error": f"未知工具 {tc.name}"}, ensure_ascii=False)
                elif decision is False:
                    out = json.dumps({"denied": True,
                                      "message": f"工具 {tc.name} 已被人工否决，禁止执行。"
                                                 "请向用户说明该操作未执行。"}, ensure_ascii=False)
                else:
                    try:
                        out = await tool.handler(tc.arguments)
                    except Exception as e:  # 工具失败回注给模型而非崩溃
                        out = json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)

                if tool is not None and tool.emits_citations:
                    try:
                        parsed = json.loads(out)
                        if isinstance(parsed, dict) and parsed.get("citations"):
                            citations.extend(parsed["citations"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            continue

        steps.append(f"step{step}({record.name}): 生成最终回答")
        return RunResult(
            content=result.content or "",
            steps=steps,
            usage=usage,
            citations=citations,
            model=last_model,
        )

    return RunResult(
        content="（已达到最大步数预算，循环终止。请缩小问题范围后重试。）",
        steps=steps + [f"budget: max_steps={max_steps} 已耗尽"],
        usage=usage,
        citations=citations,
        model=last_model,
    )

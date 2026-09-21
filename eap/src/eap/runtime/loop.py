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


def _sandbox_violation(tool_name: str, decision: dict, agent_name: str) -> None:
    """M33：tool.sandbox.violation 审计 + 指标（audit 放行 / enforce 拒绝共用）。"""
    try:
        from ..observability.audit import record as _record
        from ..observability.metrics import incr as _incr

        _record("tool.sandbox.violation", actor="system", target=tool_name,
                detail={"mode": decision.get("mode"), "policy": decision.get("policy"),
                        "action": decision.get("action"), "agent": agent_name})
        _incr("eap_sandbox_violations_total", {"mode": str(decision.get("mode") or "enforce")})
    except Exception:
        pass  # 治理观测失败不影响工具结果回注


@dataclass
class RunResult:
    content: str
    steps: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    model: str = ""
    data: dict | None = None  # 结构化输出（v0.5）：schema 校验通过的 JSON
    data_schema: dict | None = None


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
    response_schema: dict | None = None,
    agent_name: str = "",
) -> RunResult:
    """执行 Agent 循环。

    approval_gate(tool_name) -> True（执行）/ False（否决，注入拒绝结果）/ None（挂起等待人工）。
    resume_messages：HITL 恢复时传入挂起快照的完整消息历史（替代 system+messages 重建）。
    response_schema（v0.5 结构化输出）：无工具时直接约束每次补全；带工具时在最终回答后
    追加一次结构化补全（json_schema 与工具调用互斥，不能全程约束）。
    """
    usage = {"tokens_in": 0, "tokens_out": 0}
    steps: list[str] = []
    citations: list[dict] = []
    if not agent_name:
        from .policy import agent_scope

        agent_name = agent_scope.get()
    if resume_messages:
        msgs = [dict(m) for m in resume_messages]
    else:
        msgs = [{"role": "system", "content": system}, *messages]
    tool_schemas = [t.schema() for t in tools] or None
    last_model = ""

    effective_schema = response_schema if not tools else None
    for step in range(1, max_steps + 1):
        completion = await hub.complete(
            db, msgs, capability=capability, tools=tool_schemas, prefer=prefer,
            response_schema=effective_schema,
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

                # 工具治理（v0.6-①）：策略白名单/风险审批 + 每次调用审计与指标
                from .policy import PolicyDenied, check_tool, requires_tool_approval

                risk = getattr(tool, "risk_level", "low") if tool is not None else "low"
                try:
                    check_tool(db, tc.name, risk)
                except PolicyDenied as e:
                    out = json.dumps({"error": f"工具被策略拒绝: {e}"}, ensure_ascii=False)
                    if tool is not None and tool.emits_citations:
                        pass
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                    continue

                decision = True
                needs_approval = tool is not None and approval_gate is not None and (
                    tool.requires_approval or requires_tool_approval(db, tc.name, risk))
                if needs_approval:
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
                    import time as _time

                    _t0 = _time.monotonic()
                    _status = "ok"
                    # M33 沙箱路由：tool-sandbox 清单命中时按策略语义执行
                    # （位于 M24 拦截链与审批门之后）
                    from .policy import check_tool_sandbox as _check_sandbox

                    _sandbox = _check_sandbox(db, tc.name, getattr(tool, "runtime", "inproc"))
                    try:
                        import asyncio as _asyncio

                        if _sandbox is not None and _sandbox["action"] == "deny":
                            # enforce：进程内工具无法沙箱化 → 拒绝（EAP-7102）
                            raise PolicyDenied(_sandbox["reason"])
                        if _sandbox is not None and _sandbox["action"] == "sandbox":
                            # 脚本工具必须走沙箱（防绕过 handler），工具自身 timeout_s 传导
                            from .tools import run_script_in_sandbox as _run_script

                            if not getattr(tool, "script_path", None):
                                raise ValueError(f"脚本工具 {tc.name} 缺少 script_path，无法沙箱执行")
                            _t_limit = getattr(tool, "timeout_s", 30.0)
                            out = await _asyncio.wait_for(
                                _run_script(tool.script_path, tc.arguments, timeout_s=_t_limit),
                                timeout=_t_limit)
                        else:
                            if _sandbox is not None and _sandbox["action"] == "audit":
                                # audit：进程内工具放行执行，仅记违规审计与指标
                                _sandbox_violation(tc.name, _sandbox, agent_name)
                            out = await _asyncio.wait_for(tool.handler(tc.arguments),
                                                          timeout=getattr(tool, "timeout_s", 30.0))
                    except PolicyDenied as e:  # M33：enforce 沙箱拒绝 → 违规审计 + 指标
                        _status = "error"
                        out = json.dumps({"error": f"工具被策略拒绝: {e}"}, ensure_ascii=False)
                        if _sandbox is not None:
                            _sandbox_violation(tc.name, _sandbox, agent_name)
                    except Exception as e:  # 工具失败（含超时）回注给模型而非崩溃
                        _status = "error"
                        out = json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)
                    finally:
                        try:
                            from ..observability.audit import record as _audit_record
                            from ..observability.metrics import incr as _incr

                            _elapsed = int((_time.monotonic() - _t0) * 1000)
                            _audit_record("tool.call", actor="system", target=tc.name,
                                          detail={"agent": agent_name, "status": _status,
                                                  "elapsed_ms": _elapsed,
                                                  "args": tc.arguments[:500]})
                            _incr("eap_tool_calls_total", {"tool": tc.name, "status": _status})
                        except Exception:
                            pass  # 治理观测失败不影响工具结果回注

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
        usage["model"] = last_model
        if response_schema is not None and tools:
            # 结构化收尾：把最终回答整理为符合 schema 的 JSON（json_schema 与 tools 互斥）
            struct = await hub.complete(
                db, [*msgs, {"role": "user", "content": "请基于以上对话信息完成结构化输出。"}],
                capability=capability, prefer=prefer, response_schema=response_schema,
            )
            usage["tokens_in"] += struct.result.tokens_in
            usage["tokens_out"] += struct.result.tokens_out
            steps.append(f"step{step}({record.name}): 结构化输出（schema 校验）")
            return RunResult(
                content=struct.result.content or "",
                steps=steps,
                usage=usage,
                citations=citations,
                model=last_model,
                data=struct.result.data,
                data_schema=response_schema,
            )
        return RunResult(
            content=result.content or "",
            steps=steps,
            usage=usage,
            citations=citations,
            model=last_model,
            data=result.data,
            data_schema=response_schema,
        )

    return RunResult(
        content="（已达到最大步数预算，循环终止。请缩小问题范围后重试。）",
        steps=steps + [f"budget: max_steps={max_steps} 已耗尽"],
        usage={**usage, "model": last_model},
        citations=citations,
        model=last_model,
    )

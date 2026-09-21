"""多智能体运行时：Supervisor 委派 + Handoff + 递归深度护栏（docs/03 §6）。

委派实现为特殊工具（agent.<name>）：模型经工具调用把任务转给下级智能体，
下级的输出（含引用）作为工具结果回注，主管汇总。深度护栏防循环委派。
"""

from __future__ import annotations

import contextvars
import json

from .tools import Tool

_MAX_DELEGATION_DEPTH = 3
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("eap_delegate_depth", default=0)


def current_depth() -> int:
    return _depth.get()


def push_depth() -> contextvars.Token:
    """深度 +1 并返回 token（M30：A2A 外部委派工具复用同一防循环护栏）。"""
    return _depth.set(_depth.get() + 1)


def pop_depth(token: contextvars.Token) -> None:
    _depth.reset(token)


def delegate_tool(target_agent: str, description: str = "") -> Tool:
    """为指定下级智能体生成委派工具：agent.<name>(input) → 下级完整回答。"""

    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        text = str(args.get("input") or args.get("query") or "").strip()
        if not text:
            return json.dumps({"error": "缺少 input 参数"}, ensure_ascii=False)

        depth = _depth.get()
        if depth >= _MAX_DELEGATION_DEPTH:
            return json.dumps({"error": f"委派深度已达上限 {_MAX_DELEGATION_DEPTH}，请自行作答"},
                              ensure_ascii=False)

        from ..agents.registry import registry
        from ..schemas import InvokeRequest
        from ..db import SessionLocal
        from .policy import PolicyDenied, check_agent_delegation

        token = _depth.set(depth + 1)
        try:
            with SessionLocal() as db:
                check_agent_delegation(db, target_agent)  # v0.6-①：agent-allowlist 委派边界
                resp = await registry.invoke(db, target_agent, InvokeRequest(input=text))
            return json.dumps({
                "agent": target_agent,
                "output": resp.output,
                "citations": [c.model_dump() for c in resp.citations],
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"委派失败: {e}"}, ensure_ascii=False)
        finally:
            _depth.reset(token)

    return Tool(
        name=f"agent.{target_agent}",
        description=description or f"将任务委派给智能体 {target_agent} 处理，返回其回答",
        parameters={
            "type": "object",
            "properties": {"input": {"type": "string", "description": "要委派的任务/问题"}},
            "required": ["input"],
        },
        handler=handler,
        emits_citations=True,
    )


def delegate_tools(sub_agents: list[str], descriptions: dict[str, str] | None = None) -> list[Tool]:
    """按 manifest 的 sub_agents 列表生成一组委派工具。"""
    descriptions = descriptions or {}
    return [delegate_tool(n, descriptions.get(n, "")) for n in sub_agents]

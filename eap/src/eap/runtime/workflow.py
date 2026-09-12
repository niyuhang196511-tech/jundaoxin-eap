"""Workflow Engine（简单智能体的 DSL 编排，docs/03 §6）。

设计：
- 顺序步骤 + 条件跳转（branch → then_id/else_id），确定性可重放
- 节点类型：llm（可引用 Prompt 中心 + 知识检索注入）/ tool / retrieve / branch
- 变量上下文：input + 各步骤输出，"$var" 引用；条件为结构化操作（不做 eval，防注入）
- DSL 经 create_workflow_agent_class 包装为 AgentApp → 走统一注册/调用/嵌入外链
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..agents.app import AgentApp
from ..schemas import AgentManifest, InvokeRequest, InvokeResult
from .tools import Tool


# ---------- DSL 模型 ----------

class Condition(BaseModel):
    left: str | None = None
    op: Literal["contains", "eq", "ne", "empty", "not_empty"]
    right: str | None = None


class Step(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    type: Literal["llm", "tool", "retrieve", "branch"]
    when: Condition | None = None  # 条件不满足则跳过本步骤

    # llm
    system: str = ""
    prompt_name: str | None = None  # Prompt 中心引用（优先于 system）
    prompt_vars: dict[str, str] = Field(default_factory=dict)  # 值支持 "$var"
    model: str = "auto"
    knowledge: list[str] = Field(default_factory=list)  # 检索注入的 KB
    query_var: str = "input"

    # tool / retrieve
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    kb: str | None = None
    query_var_alt: str | None = None
    top_k: int = 3

    # branch
    left: str | None = None
    op: Literal["contains", "eq", "ne", "empty", "not_empty"] | None = None
    right: str | None = None
    then_id: str | None = None
    else_id: str | None = None


class WorkflowSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = ""
    steps: list[Step] = Field(min_length=1)

    @property
    def knowledge_refs(self) -> list[str]:
        seen: dict[str, None] = {}
        for s in self.steps:
            for kb in s.knowledge:
                seen[kb] = None
        return list(seen)


# ---------- 变量与条件 ----------

def resolve(value: str | None, variables: dict) -> Any:
    """"$name" 引用上下文变量，否则字面量。"""
    if isinstance(value, str) and value.startswith("$"):
        return variables.get(value[1:], "")
    return value


def eval_condition(cond: Condition, variables: dict) -> bool:
    left = resolve(cond.left, variables)
    if cond.op == "empty":
        return not str(left or "").strip()
    if cond.op == "not_empty":
        return bool(str(left or "").strip())
    right = resolve(cond.right, variables)
    if cond.op == "contains":
        return str(right) in str(left or "")
    if cond.op == "eq":
        return str(left) == str(right)
    if cond.op == "ne":
        return str(left) != str(right)
    raise ValueError(f"未知条件操作: {cond.op}")


# ---------- 工具解析 ----------

_WORKFLOW_TOOLS: dict[str, callable] = {}


def register_workflow_tool(name: str, factory) -> None:
    """注册工作流可用工具（工厂签名 () -> Tool）。"""
    _WORKFLOW_TOOLS[name] = factory


def resolve_tool(name: str) -> Tool:
    import re

    m = re.match(r"^kb\.([a-z0-9-]+)\.search$", name)
    if m:
        from .agents.runtime_tools import retriever_tool

        return retriever_tool(m.group(1))
    if name in _WORKFLOW_TOOLS:
        return _WORKFLOW_TOOLS[name]()
    raise ValueError(f"工作流工具 {name} 未注册（可用: kb.<name>.search 或已注册工具）")


# ---------- 执行引擎 ----------

async def execute_workflow(spec: WorkflowSpec, ctx, db, invoke_input: str) -> dict:
    """确定性执行：返回 {output, citations, steps}。每步输出可重放。"""
    from .context import build_system, render_hits

    variables: dict[str, Any] = {"input": invoke_input}
    citations: list[dict] = []
    trace: list[str] = []
    output = ""

    index = 0
    steps_by_id = {s.id: i for i, s in enumerate(spec.steps)}
    while index < len(spec.steps):
        step = spec.steps[index]
        index += 1

        if step.when is not None and not eval_condition(step.when, variables):
            trace.append(f"{step.id}({step.type}): skipped（when 不满足）")
            continue

        if step.type == "branch":
            taken = eval_condition(
                Condition(left=step.left, op=step.op, right=step.right), variables)
            target = step.then_id if taken else step.else_id
            trace.append(f"{step.id}(branch): {'then' if taken else 'else'} → {target}")
            if target is None:
                continue
            if target not in steps_by_id:
                raise ValueError(f"branch 目标步骤 {target} 不存在")
            index = steps_by_id[target]
            continue

        if step.type == "retrieve":
            kb_name = resolve(step.kb, variables)
            query = str(resolve(step.query_var_alt or step.query_var, variables))
            retriever = ctx.retriever(kb_name)
            hits = retriever.search(db, query, top_k=step.top_k)
            variables[step.id] = render_hits(hits)
            citations.extend(h["citation"] for h in hits)
            trace.append(f"{step.id}(retrieve): {len(hits)} hits from {kb_name}")
            continue

        if step.type == "tool":
            tool = resolve_tool(resolve(step.tool_name, variables))
            args = {k: resolve(v, variables) for k, v in step.tool_args.items()}
            out = await tool.handler(json.dumps(args, ensure_ascii=False))
            variables[step.id] = out
            trace.append(f"{step.id}(tool): {tool.name}")
            continue

        if step.type == "llm":
            knowledge_context = ""
            if step.knowledge:
                query = str(resolve(step.query_var, variables))
                parts, hits_all = [], []
                for kb_name in step.knowledge:
                    hits = ctx.retriever(kb_name).search(db, query, top_k=3)
                    hits_all.extend(hits)
                    parts.append(render_hits(hits))
                knowledge_context = "\n\n".join(parts)
                citations.extend(h["citation"] for h in hits_all)

            if step.prompt_name:
                system = ctx.prompt(step.prompt_name,
                                    {k: str(resolve(v, variables))
                                     for k, v in step.prompt_vars.items()} | {"input": invoke_input})
            else:
                system = build_system(
                    role=step.system or "你是企业智能助手。",
                    knowledge_context=knowledge_context,
                    max_chars=ctx.settings.max_context_chars,
                )
            completion = await ctx.chat(
                db,
                messages=[{"role": "user", "content": invoke_input}],
                system=system,
                prefer=None if step.model == "auto" else step.model,
            )
            result, record = completion.result, completion.record
            output = result.content or ""
            variables[step.id] = output
            trace.append(f"{step.id}(llm via {record.name}): {len(output)} chars")
            continue

        raise ValueError(f"未知步骤类型: {step.type}")

    return {"output": output, "citations": citations, "steps": trace}


# ---------- 动态注册为智能体 ----------

def create_workflow_agent_class(spec: WorkflowSpec) -> type:
    """把 DSL 包装为 AgentApp 子类（闭包持有 spec）→ 统一注册/调用/嵌入。"""

    class WorkflowAgent(AgentApp):
        async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
            with self.ctx.db() as db:
                result = await execute_workflow(spec, self.ctx, db, request.input)
                return InvokeResult(
                    content=result["output"],
                    citations=result["citations"],
                    steps=result["steps"],
                )

    manifest = AgentManifest(
        name=spec.name,
        version=spec.version,
        description=spec.description or f"工作流智能体：{len(spec.steps)} 个步骤",
        knowledge=spec.knowledge_refs,
        permissions=["kb.retrieve"],
        embeddable=True,
        domains=["*"],
    )
    WorkflowAgent.manifest = manifest
    WorkflowAgent.__name__ = f"WorkflowAgent_{spec.name}"
    return WorkflowAgent

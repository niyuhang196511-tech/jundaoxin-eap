"""Workflow Engine（简单智能体的 DSL 编排，docs/03 §6）。

设计：
- 顺序步骤 + 条件跳转（branch → then_id/else_id），确定性可重放
- 节点类型：llm（可引用 Prompt 中心 + 知识检索注入）/ tool / retrieve / branch
- 变量上下文：input + 各步骤输出，"$var" 引用；条件为结构化操作（不做 eval，防注入）
- DSL 经 create_workflow_agent_class 包装为 AgentApp → 走统一注册/调用/嵌入外链
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    type: Literal["llm", "tool", "retrieve", "branch", "parallel", "subflow"]
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

    # parallel：分支并发各跑线性子序列；分支内仅 llm/tool/retrieve（保确定性重放）
    branches: list["ParallelBranch"] = Field(default_factory=list)
    join_with: str = "\n\n"  # 分支输出拼接符（列表另存 "$<id>.items"）

    # subflow：调用另一个已注册工作流（深度护栏防环）
    workflow: str | None = None
    input_var: str = "input"


class ParallelBranch(BaseModel):
    """并行分支：一个线性子序列（llm/tool/retrieve），与其他分支并发执行。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _inner_types(self) -> "ParallelBranch":
        for s in self.steps:
            if s.type not in _PARALLEL_INNER_TYPES:
                raise ValueError(
                    f"parallel 分支 {self.id} 不允许步骤类型 {s.type}（仅 llm/tool/retrieve）")
        return self


# 并发分支内允许的步骤类型（分支里再嵌 branch/parallel/subflow 会破坏确定性重放）
_PARALLEL_INNER_TYPES = frozenset({"llm", "tool", "retrieve"})

# subflow 深度护栏：A→B→A 循环引用时截断（docs/03 §6 与多智能体委派护栏同构）
_subflow_depth: contextvars.ContextVar[int] = contextvars.ContextVar("eap_subflow_depth", default=0)
_MAX_SUBFLOW_DEPTH = 3


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

        if step.type == "parallel":
            if not step.branches:
                raise ValueError(f"parallel 步骤 {step.id} 缺少 branches")
            results = await asyncio.gather(
                *(_run_linear_branch(b, ctx, db, variables) for b in step.branches))
            outputs, items = [], []
            for branch, (out, branch_citations, branch_trace) in zip(step.branches, results):
                items.append({"branch": branch.id, "output": out})
                citations.extend(branch_citations)
                trace.extend(f"{step.id}/{line}" for line in branch_trace)
                outputs.append(out)
            variables[step.id] = step.join_with.join(outputs)
            variables[f"{step.id}.items"] = items
            trace.append(f"{step.id}(parallel): {len(results)} branches joined")
            continue

        if step.type == "subflow":
            target = resolve(step.workflow, variables)
            if not target:
                raise ValueError(f"subflow 步骤 {step.id} 缺少 workflow 名称")
            depth = _subflow_depth.get()
            if depth >= _MAX_SUBFLOW_DEPTH:
                trace.append(f"{step.id}(subflow): 深度达上限 {depth}，跳过（防环）")
                continue
            from ..agents.registry import registry
            from ..schemas import InvokeRequest as _Req

            token = _subflow_depth.set(depth + 1)
            try:
                sub_input = str(resolve(step.input_var, variables))
                resp = await registry.invoke(db, str(target), _Req(input=sub_input))
            except KeyError as e:
                raise ValueError(f"subflow 目标工作流 {target} 未注册") from e
            finally:
                _subflow_depth.reset(token)
            variables[step.id] = resp.output
            citations.extend(c.model_dump() for c in resp.citations)
            trace.append(f"{step.id}(subflow → {target}): {len(resp.output)} chars")
            continue

        if step.type in ("retrieve", "tool", "llm"):
            out, step_citations, line = await _run_simple_step(step, ctx, db, variables, invoke_input)
            variables[step.id] = out
            citations.extend(step_citations)
            trace.append(line)
            output = out  # 最后一个线性步骤的输出即工作流输出
            continue

        raise ValueError(f"未知步骤类型: {step.type}")

    return {"output": output, "citations": citations, "steps": trace}


async def _run_simple_step(step: Step, ctx, db, variables: dict, invoke_input: str) -> tuple[str, list[dict], str]:
    """线性单步（llm/tool/retrieve）：并行分支与主循环共用。"""
    from .context import build_system, render_hits

    if step.type == "retrieve":
        kb_name = resolve(step.kb, variables)
        query = str(resolve(step.query_var_alt or step.query_var, variables))
        hits = ctx.retriever(kb_name).search(db, query, top_k=step.top_k)
        citations = [h["citation"] for h in hits]
        return render_hits(hits), citations, f"{step.id}(retrieve): {len(hits)} hits from {kb_name}"

    if step.type == "tool":
        tool = resolve_tool(resolve(step.tool_name, variables))
        args = {k: resolve(v, variables) for k, v in step.tool_args.items()}
        out = await tool.handler(json.dumps(args, ensure_ascii=False))
        return out, [], f"{step.id}(tool): {tool.name}"

    # llm
    knowledge_context = ""
    citations: list[dict] = []
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
    return output, citations, f"{step.id}(llm via {record.name}): {len(output)} chars"


async def _run_linear_branch(branch: "ParallelBranch", ctx, db, variables: dict) -> tuple[str, list[dict], list[str]]:
    """并行分支内线性执行（仅 llm/tool/retrieve）；分支变量独立（不回写主上下文）。"""
    local_vars = dict(variables)
    citations: list[dict] = []
    trace: list[str] = []
    output = ""
    for step in branch.steps:
        if step.type not in _PARALLEL_INNER_TYPES:
            raise ValueError(
                f"parallel 分支 {branch.id} 不允许步骤类型 {step.type}（仅 llm/tool/retrieve）")
        output, step_citations, line = await _run_simple_step(step, ctx, db, local_vars,
                                                              str(variables.get("input", "")))
        local_vars[step.id] = output
        citations.extend(step_citations)
        trace.append(f"{branch.id}:{line}")
    return output, citations, trace


# ---------- 动态注册为智能体 ----------

def create_workflow_agent_class(spec: WorkflowSpec) -> type:
    """把 DSL 包装为 AgentApp 子类（闭包持有 spec）→ 统一注册/调用/嵌入。"""

    class WorkflowAgent(AgentApp):
        async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
            with self.ctx.db() as db:
                try:
                    result = await execute_workflow(spec, self.ctx, db, request.input)
                except ValueError as e:
                    # DSL 错误（subflow 目标缺失等）→ RuntimeError → 端点统一映射 503 EAP-4005
                    raise RuntimeError(f"EAP-4005 {e}") from e
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

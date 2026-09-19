"""Workflow Engine（docs/03 §6，DSL v2：线性步骤 + 图边两种形态）。

设计：
- DSL v2：`edges` 存在时走图遍历执行（画布拖线编排）；否则保持线性 steps 兼容
  （branch → then_id/else_id 跳转）。旧 DSL 加载时由前端/服务端自动转边。
- 节点类型：llm（Prompt 中心 + KB 注入）/ tool / retrieve / branch / parallel / subflow / loop
- 变量上下文：input + 各节点输出，"$var" 引用；条件为结构化操作（不做 eval，防注入）
- 事件回调：on_event 逐节点发出 start/end（试运行状态可视化 + 运行记录落库）
- DSL 经 create_workflow_agent_class 包装为 AgentApp → 走统一注册/调用/嵌入外链
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from ..agents.app import AgentApp
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from ..schemas import AgentManifest, InvokeRequest, InvokeResult
from .tools import Tool


# ---------- DSL 模型 ----------

class Condition(BaseModel):
    left: str | None = None
    op: Literal["contains", "eq", "ne", "empty", "not_empty"]
    right: str | None = None


class NodePosition(BaseModel):
    """画布坐标（图编排布局持久化）。"""

    x: float = 0
    y: float = 0


class Step(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    type: Literal["llm", "tool", "retrieve", "branch", "parallel", "subflow", "loop"]
    when: Condition | None = None  # 条件不满足则跳过本节点
    title: str = ""  # 画布节点显示名（空则用 id）
    position: NodePosition | None = None  # 画布布局

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
    then_id: str | None = None  # 线性形态跳转；图形态走 edges.source_handle
    else_id: str | None = None

    # parallel：分支并发各跑线性子序列；分支内仅 llm/tool/retrieve（保确定性重放）
    branches: list["ParallelBranch"] = Field(default_factory=list)
    join_with: str = "\n\n"  # 分支/循环输出拼接符（列表另存 "$<id>.items"）

    # subflow：调用另一个已注册工作流（深度护栏防环）
    workflow: str | None = None
    input_var: str = "input"

    # loop：对数组变量逐项执行线性 body（约束同 parallel 分支）
    loop_var: str | None = None  # 如 "$x.items"
    item_var: str = "item"  # 迭代变量写入上下文名
    max_iterations: int = Field(default=20, ge=1, le=200)
    body: list["Step"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_node(self) -> "Step":
        if self.type == "loop":
            if not self.loop_var:
                raise ValueError(f"loop 节点 {self.id} 缺少 loop_var")
            if not self.body:
                raise ValueError(f"loop 节点 {self.id} 缺少 body")
            for s in self.body:
                if s.type not in _PARALLEL_INNER_TYPES:
                    raise ValueError(
                        f"loop 节点 {self.id} 的 body 不允许步骤类型 {s.type}（仅 llm/tool/retrieve）")
        return self


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


class WorkflowEdge(BaseModel):
    """图形态控制流边：source_handle 标注 branch 出口（then/else）。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,60}$")
    source: str
    target: str
    source_handle: Literal["then", "else"] | None = None


# 并发/循环体内允许的步骤类型（再嵌 branch/parallel/subflow/loop 会破坏确定性重放）
_PARALLEL_INNER_TYPES = frozenset({"llm", "tool", "retrieve"})

# subflow 深度护栏：A→B→A 循环引用时截断（docs/03 §6 与多智能体委派护栏同构）
_subflow_depth: contextvars.ContextVar[int] = contextvars.ContextVar("eap_subflow_depth", default=0)
_MAX_SUBFLOW_DEPTH = 3


class WorkflowSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = ""
    steps: list[Step] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)  # 非空 → 图遍历执行

    @property
    def knowledge_refs(self) -> list[str]:
        seen: dict[str, None] = {}
        for s in self.steps:
            for kb in s.knowledge:
                seen[kb] = None
        return list(seen)

    @model_validator(mode="after")
    def _validate_graph(self) -> "WorkflowSpec":
        if not self.edges:
            return self
        ids = {s.id for s in self.steps}
        seen_pairs: set[tuple[str, str, str | None]] = set()
        for e in self.edges:
            if e.source not in ids or e.target not in ids:
                raise ValueError(f"边 {e.id} 引用了不存在的节点（{e.source} → {e.target}）")
            key = (e.source, e.target, e.source_handle)
            if key in seen_pairs:
                raise ValueError(f"重复边：{e.source} → {e.target}（{e.source_handle}）")
            seen_pairs.add(key)
        return self


def linear_to_edges(steps: list[Step]) -> list[WorkflowEdge]:
    """旧线性 DSL → 等价图边（画布载入旧工作流时可视化编辑）。"""
    edges: list[WorkflowEdge] = []
    steps_by_id = {s.id: i for i, s in enumerate(steps)}
    for i, s in enumerate(steps):
        if s.type == "branch":
            for handle, target in (("then", s.then_id), ("else", s.else_id)):
                if target and target in steps_by_id:
                    edges.append(WorkflowEdge(
                        id=f"e{i}-{handle}-{target}", source=s.id, target=target,
                        source_handle=handle))
        elif i + 1 < len(steps):
            edges.append(WorkflowEdge(id=f"e{i}-next", source=s.id, target=steps[i + 1].id))
    return edges


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
    """工具统一解析（v0.5-⑥ Action 复用）：kb 检索 / 工作流注册表 / 连接器 / 插件。"""
    import re

    m = re.match(r"^kb\.([a-z0-9-]+)\.search$", name)
    if m:
        from .agents.runtime_tools import retriever_tool

        return retriever_tool(m.group(1))
    if name in _WORKFLOW_TOOLS:
        return _WORKFLOW_TOOLS[name]()
    # 连接器工具（Action/UI 按钮按 tool_name 调用，如 erp.inventory.query）
    from ..db import SessionLocal
    from ..models import ConnectorRecord
    from .connectors import load_connector_tools

    try:
        with SessionLocal() as db:
            for record in db.scalars(
                    select(ConnectorRecord).where(ConnectorRecord.enabled == True)).all():  # noqa: E712
                for tool in load_connector_tools(record):
                    if tool.name == name:
                        return tool
    except OperationalError:
        pass  # 表未建（无库环境直调 resolve_tool）——按未注册处理
    raise ValueError(f"工具 {name} 未注册（可用: kb.<name>.search / 已注册工作流工具 / 连接器工具）")


# ---------- 执行引擎 ----------

WorkflowEvent = dict  # {node, node_type, type: start|end, status, output, error, elapsed_ms}
EventSink = Callable[[WorkflowEvent], Awaitable[None]]


class _RunState:
    """单次执行的可变状态：变量上下文 + 引用 + trace + 输出。"""

    def __init__(self, invoke_input: str) -> None:
        self.variables: dict[str, Any] = {"input": invoke_input}
        self.citations: list[dict] = []
        self.trace: list[str] = []
        self.output = ""


async def _emit(on_event: EventSink | None, **ev: Any) -> None:
    if on_event is None:
        return
    try:
        await on_event(ev)
    except Exception:
        pass  # 事件回调（试运行落库）不中断执行


async def execute_workflow(
    spec: WorkflowSpec, ctx, db, invoke_input: str, on_event: EventSink | None = None,
) -> dict:
    """确定性执行：返回 {output, citations, steps}。edges 非空走图遍历，否则线性。"""
    state = _RunState(invoke_input)
    if spec.edges:
        await _execute_graph(spec, ctx, db, invoke_input, state, on_event)
    else:
        await _execute_linear(spec, ctx, db, invoke_input, state, on_event)
    return {"output": state.output, "citations": state.citations, "steps": state.trace}


async def _execute_linear(
    spec: WorkflowSpec, ctx, db, invoke_input: str, state: _RunState, on_event: EventSink | None,
) -> None:
    index = 0
    steps_by_id = {s.id: i for i, s in enumerate(spec.steps)}
    while index < len(spec.steps):
        step = spec.steps[index]
        index += 1

        if step.when is not None and not eval_condition(step.when, state.variables):
            state.trace.append(f"{step.id}({step.type}): skipped（when 不满足）")
            continue

        if step.type == "branch":
            taken = eval_condition(
                Condition(left=step.left, op=step.op, right=step.right), state.variables)
            target = step.then_id if taken else step.else_id
            state.trace.append(f"{step.id}(branch): {'then' if taken else 'else'} → {target}")
            if target is None:
                continue
            if target not in steps_by_id:
                raise ValueError(f"branch 目标步骤 {target} 不存在")
            index = steps_by_id[target]
            continue

        started = time.perf_counter()
        await _emit(on_event, node=step.id, node_type=step.type, type="start")
        out = await _execute_node(step, ctx, db, state, invoke_input)
        elapsed = int((time.perf_counter() - started) * 1000)
        state.output = out  # 最后一个线性节点的输出即工作流输出
        await _emit(on_event, node=step.id, node_type=step.type, type="end",
                    status="ok", output=str(out)[:4000], elapsed_ms=elapsed)


async def _execute_graph(
    spec: WorkflowSpec, ctx, db, invoke_input: str, state: _RunState, on_event: EventSink | None,
) -> None:
    steps_by_id = {s.id: s for s in spec.steps}
    outgoing: dict[str, list[WorkflowEdge]] = {}
    incoming: set[str] = set()
    for e in spec.edges:
        outgoing.setdefault(e.source, []).append(e)
        incoming.add(e.target)

    # 起点：无入边节点（多条取声明序首个）；全成环时退回首个声明节点
    start = next((s.id for s in spec.steps if s.id not in incoming), None)
    if start is None:
        raise ValueError("工作流图存在全环：没有可用的起始节点")

    current: str | None = start
    hops = 0
    max_hops = max(len(spec.steps) * 4, 64)  # 图跳数护栏（branch 回跳也要覆盖）
    while current is not None:
        hops += 1
        if hops > max_hops:
            raise ValueError(f"图执行跳数超过上限 {max_hops}（疑似无法收敛的环）")
        step = steps_by_id.get(current)
        if step is None:
            raise ValueError(f"边引用了不存在的节点 {current}")

        if step.when is not None and not eval_condition(step.when, state.variables):
            state.trace.append(f"{step.id}({step.type}): skipped（when 不满足）")
            nxt = outgoing.get(step.id)
            current = nxt[0].target if nxt else None
            continue

        if step.type == "branch":
            started = time.perf_counter()
            await _emit(on_event, node=step.id, node_type=step.type, type="start")
            taken = eval_condition(
                Condition(left=step.left, op=step.op, right=step.right), state.variables)
            handle = "then" if taken else "else"
            candidates = [e for e in outgoing.get(step.id, []) if e.source_handle == handle]
            if not candidates:  # 未标注出口的边一律视作 then
                candidates = [e for e in outgoing.get(step.id, []) if e.source_handle is None]
            target = candidates[0].target if candidates else None
            elapsed = int((time.perf_counter() - started) * 1000)
            state.trace.append(f"{step.id}(branch): {handle} → {target}")
            await _emit(on_event, node=step.id, node_type=step.type, type="end",
                        status="ok", output=handle, elapsed_ms=elapsed)
            current = target
            continue

        started = time.perf_counter()
        await _emit(on_event, node=step.id, node_type=step.type, type="start")
        out = await _execute_node(step, ctx, db, state, invoke_input)
        elapsed = int((time.perf_counter() - started) * 1000)
        state.trace.append(f"{step.id}({step.type}): ok")
        state.output = out
        await _emit(on_event, node=step.id, node_type=step.type, type="end",
                    status="ok", output=str(out)[:4000], elapsed_ms=elapsed)
        nxt = outgoing.get(step.id)
        current = nxt[0].target if nxt else None


async def _execute_node(step: Step, ctx, db, state: _RunState, invoke_input: str) -> str:
    """单节点执行（llm/tool/retrieve/parallel/subflow/loop），返回节点输出。"""
    variables, citations, trace = state.variables, state.citations, state.trace

    if step.type == "parallel":
        if not step.branches:
            raise ValueError(f"parallel 节点 {step.id} 缺少 branches")
        results = await asyncio.gather(
            *(_run_linear_branch(b, ctx, db, variables, invoke_input) for b in step.branches))
        outputs, items = [], []
        for branch, (out, branch_citations, branch_trace) in zip(step.branches, results):
            items.append({"branch": branch.id, "output": out})
            citations.extend(branch_citations)
            trace.extend(f"{step.id}/{line}" for line in branch_trace)
            outputs.append(out)
        joined = step.join_with.join(outputs)
        variables[step.id] = joined
        variables[f"{step.id}.items"] = items
        trace.append(f"{step.id}(parallel): {len(results)} branches joined")
        return joined

    if step.type == "subflow":
        target = resolve(step.workflow, variables)
        if not target:
            raise ValueError(f"subflow 节点 {step.id} 缺少 workflow 名称")
        depth = _subflow_depth.get()
        if depth >= _MAX_SUBFLOW_DEPTH:
            trace.append(f"{step.id}(subflow): 深度达上限 {depth}，跳过（防环）")
            return ""
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
        return resp.output

    if step.type == "loop":
        arr = resolve(step.loop_var, variables)
        if isinstance(arr, str):  # 工具节点输出是文本：尝试解析 JSON 数组
            try:
                parsed = json.loads(arr)
                if isinstance(parsed, list):
                    arr = parsed
            except json.JSONDecodeError:
                pass
        if not isinstance(arr, list):
            raise ValueError(f"loop 节点 {step.id} 的 loop_var {step.loop_var} 不是数组")
        outputs, items = [], []
        for i, item in enumerate(arr[: step.max_iterations]):
            local = dict(variables)
            local[step.item_var] = item
            out_i = ""
            for body_step in step.body:
                out_i, cits, line = await _run_simple_step(body_step, ctx, db, local, invoke_input)
                local[body_step.id] = out_i
                citations.extend(cits)
                trace.append(f"{step.id}[{i}]:{line}")
            outputs.append(out_i)
            items.append({"index": i, "output": out_i})
        joined = step.join_with.join(str(o) for o in outputs)
        variables[step.id] = joined
        variables[f"{step.id}.items"] = items
        trace.append(f"{step.id}(loop): {len(outputs)}/{len(arr)} items")
        return joined

    if step.type in ("retrieve", "tool", "llm"):
        out, step_citations, line = await _run_simple_step(step, ctx, db, variables, invoke_input)
        variables[step.id] = out
        citations.extend(step_citations)
        trace.append(line)
        return out

    raise ValueError(f"未知步骤类型: {step.type}")


async def _run_simple_step(step: Step, ctx, db, variables: dict, invoke_input: str) -> tuple[str, list[dict], str]:
    """线性单步（llm/tool/retrieve）：并行分支、循环体与主循环共用。"""
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


async def _run_linear_branch(
    branch: "ParallelBranch", ctx, db, variables: dict, invoke_input: str,
) -> tuple[str, list[dict], list[str]]:
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
                                                              invoke_input)
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
        description=spec.description or f"工作流智能体：{len(spec.steps)} 个节点",
        knowledge=spec.knowledge_refs,
        permissions=["kb.retrieve"],
        embeddable=True,
        domains=["*"],
    )
    WorkflowAgent.manifest = manifest
    WorkflowAgent.__name__ = f"WorkflowAgent_{spec.name}"
    return WorkflowAgent

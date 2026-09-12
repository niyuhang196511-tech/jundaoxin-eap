"""下单助手智能体：演示 HITL —— 下单工具需人工审批，任务挂起等待批准后执行。

直接调用（on_invoke）不带审批门控（开发便利）；经任务引擎（agent.hitl）执行时强制审批。
"""

from __future__ import annotations

import json

from ..app import AgentApp
from ..manifest import AgentManifest
from ...runtime.context import build_system, citations_from
from ...runtime.tools import Tool
from ..sdk import register_agent
from ...schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="order-agent",
    version="1.0.0",
    description="下单助手：产品问答 + 下单（下单工具需人工审批，经任务引擎执行）",
    models=[{"capability": "chat", "required": True}],
    knowledge=["product-docs"],
    tools=["erp.order.create"],
    permissions=["order.read", "order.create"],
)

SYSTEM_ROLE = ("你是销售下单助手。涉及下单的请求必须先调用 erp.order.create 工具；"
               "产品咨询仅依据资料回答并标注 [n]。")


def order_create_tool() -> Tool:
    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        product = args.get("product") or args.get("query") or "未指定产品"
        order_id = "SO-2026-" + product[:6].upper().replace(" ", "")
        return json.dumps({
            "order_id": order_id, "status": "created",
            "product": product, "qty": args.get("qty", 1),
        }, ensure_ascii=False)

    return Tool(
        name="erp.order.create",
        description="在 ERP 创建销售订单（写操作，需人工审批）",
        parameters={
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "产品名称"},
                "qty": {"type": "integer", "description": "数量"},
            },
            "required": [],
        },
        handler=handler,
        requires_approval=True,
    )


@register_agent(MANIFEST, source="builtin")
class OrderAgent(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        return await self._run(request, gate=None, resume=None)

    async def on_invoke_task(self, request: InvokeRequest, gate=None, resume: dict | None = None) -> InvokeResult:
        approvals = dict((resume or {}).get("approvals", {}))

        def gate(tool_name: str):
            return approvals.get(tool_name)  # True/False/None

        return await self._run(request, gate=gate, resume=resume)

    async def _run(self, request: InvokeRequest, gate, resume) -> InvokeResult:
        with self.ctx.db() as db:
            retriever = self.ctx.retriever("product-docs")
            hits = retriever.search(db, request.input, top_k=2)
            system = build_system(
                role=SYSTEM_ROLE,
                knowledge_context=retriever.render(hits),
                max_chars=self.ctx.settings.max_context_chars,
            )
            run = await self.ctx.run_loop(
                db,
                messages=[{"role": "user", "content": request.input}],
                system=system,
                tools=[order_create_tool(), retriever.as_tool()],
                approval_gate=gate,
                resume_messages=(resume or {}).get("messages"),
            )
            return InvokeResult(
                content=run.content,
                citations=citations_from(run.citations) + [h["citation"] for h in hits
                                                           if h["citation"] not in run.citations],
                steps=run.steps,
                usage=run.usage,
            )

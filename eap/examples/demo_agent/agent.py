"""示例：手写智能体（销售助手）—— 演示注册钩子 + 知识库检索 + 引用溯源。

平台启动时经 EAP_AGENT_MODULES 自动发现本模块（import 即触发注册钩子）。

启动示例（在 eap/ 目录）：
    EAP_AGENT_MODULES='["examples.demo_agent.agent"]' uv run uvicorn eap.main:app
"""

from __future__ import annotations

from eap.agents.app import AgentApp
from eap.agents.manifest import AgentManifest
from eap.agents.sdk import register_agent
from eap.runtime.context import build_system
from eap.schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="sales-helper",
    version="0.1.0",
    description="示例手写智能体：绑定 product-docs 知识库的销售助手",
    models=[{"capability": "chat", "required": True}],
    knowledge=["product-docs"],
    permissions=["kb.retrieve"],
)


@register_agent(MANIFEST)
class SalesHelper(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        with self.ctx.db() as db:
            retriever = self.ctx.retriever("product-docs")
            hits = retriever.search(db, request.input, top_k=3)
            system = build_system(
                role="你是销售助手，仅依据资料回答并标注 [n] 引用。",
                knowledge_context=retriever.render(hits),
                max_chars=self.ctx.settings.max_context_chars,
            )
            completion = await self.ctx.chat(
                db,
                messages=[{"role": "user", "content": request.input}],
                system=system,
            )
            result, record = completion.result, completion.record
            return InvokeResult(
                content=result.content or "",
                citations=[h["citation"] for h in hits],
                steps=[f"retrieve: {len(hits)} hits from product-docs",
                       f"answer via {record.name}"],
                usage={"tokens_in": result.tokens_in, "tokens_out": result.tokens_out},
            )

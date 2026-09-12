"""客服主管智能体：演示多智能体协作（Supervisor 模式）。

收到请求后按意图委派给下级专家智能体（faq-agent / order-agent），汇总其回答。
经任务引擎执行时支持 HITL（委派 order-agent 的下单工具同样走审批）。
"""

from __future__ import annotations

from ..app import AgentApp
from ..manifest import AgentManifest
from ...runtime.multi_agent import delegate_tools
from ..sdk import register_agent
from ...schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="support-supervisor",
    version="1.0.0",
    description="客服主管：按意图把请求委派给 faq-agent / order-agent 并汇总回答",
    models=[{"capability": "chat", "required": True}],
    sub_agents=["faq-agent", "order-agent"],
    permissions=["delegate"],
)

SYSTEM_ROLE = (
    "你是客服主管。把用户请求委派给合适的专家智能体处理："
    "咨询类委派给 agent.faq-agent，下单类委派给 agent.order-agent；"
    "拿到专家回答后向用户简要转述。"
)


@register_agent(MANIFEST, source="builtin")
class SupportSupervisor(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        return await self._run(request, gate=None, resume=None)

    async def on_invoke_task(self, request: InvokeRequest, gate=None, resume: dict | None = None) -> InvokeResult:
        approvals = dict((resume or {}).get("approvals", {}))

        def gate(tool_name: str):
            return approvals.get(tool_name)

        return await self._run(request, gate=gate, resume=resume)

    async def _run(self, request: InvokeRequest, gate, resume) -> InvokeResult:
        with self.ctx.db() as db:
            tools = delegate_tools(MANIFEST.sub_agents)
            run = await self.ctx.run_loop(
                db,
                messages=[{"role": "user", "content": request.input}],
                system=SYSTEM_ROLE,
                tools=tools,
                approval_gate=gate,
                resume_messages=(resume or {}).get("messages"),
            )
            return InvokeResult(
                content=run.content,
                citations=[c for c in run.citations if isinstance(c, dict)],
                steps=run.steps,
                usage=run.usage,
            )

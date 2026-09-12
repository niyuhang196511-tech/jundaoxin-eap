"""多智能体测试：Supervisor 委派 / 递归深度护栏。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_supervisor_registered(client: TestClient):
    names = [a["name"] for a in client.get("/api/v1/agents", headers=AUTH).json()]
    assert "support-supervisor" in names


def test_supervisor_delegates_and_summarizes(client: TestClient):
    """mock 模型调用第一个工具（委派 faq-agent）→ 子回答回注 → 主管汇总。"""
    resp = client.post("/api/v1/agents/support-supervisor/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "根据工具返回" in data["output"], "主管应汇总委派结果"
    assert "faq-agent" in data["output"] or any("faq" in s for s in data["steps"])
    # 子智能体的引用经 emits_citations 透传到主管
    assert any(c["kb"] == "website-faq" for c in data["citations"])


def test_delegation_depth_guard(client: TestClient):
    """自委派达到深度上限 → 工具返回错误，不无限递归。"""
    import asyncio

    from eap.agents.app import AgentApp
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import get_platform_context, registry
    from eap.db import SessionLocal
    from eap.runtime.multi_agent import delegate_tools
    from eap.schemas import InvokeRequest, InvokeResult

    manifest = AgentManifest(name="self-loop", version="1.0.0")

    class LoopAgent(AgentApp):
        async def on_invoke(self, request: InvokeRequest):
            with SessionLocal() as db:
                run = await self.ctx.run_loop(
                    db, [{"role": "user", "content": request.input}],
                    system="测试", tools=delegate_tools(["self-loop"]),
                )
                return InvokeResult(content=run.content, steps=run.steps)

    registry.register(LoopAgent, manifest)
    agent = registry.get("self-loop")
    agent.instance = LoopAgent(get_platform_context())

    with SessionLocal() as db:
        result = asyncio.run(agent.instance.on_invoke(InvokeRequest(input="自我委派")))
    assert "上限" in result.content or "委派深度" in result.content

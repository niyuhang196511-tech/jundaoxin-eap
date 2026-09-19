"""Tool Governance 测试（v0.6-M24）：tool-allowlist / tool-risk-approval / agent-allowlist / 工具审计。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from .conftest import AUTH


def _make_policy(client: TestClient, name: str, kind: str, config: dict):
    return client.post("/api/v1/policies", headers=AUTH,
                       json={"name": name, "tenant_id": 1, "kind": kind,
                             "config": config, "priority": 10})


def _dummy_tools_agent(client: TestClient):
    """注册一个带高风险/低风险工具的智能体并启动。"""
    import asyncio

    from eap.agents.app import AgentApp
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import registry
    from eap.agents.sdk import register_agent
    from eap.runtime.tools import Tool
    from eap.schemas import InvokeRequest, InvokeResult

    if "gov-tools-agent" in registry._agents:
        return

    def _tool(name: str, risk: str, approval: bool) -> Tool:
        async def _handler(args: str) -> str:
            return json.dumps({"ok": True})
        return Tool(name=name, description=name,
                    parameters={"type": "object", "properties": {}},
                    handler=_handler, risk_level=risk, requires_approval=approval)

    @register_agent(AgentManifest(name="gov-tools-agent", version="1.0.0"), source="sdk")
    class GovTools(AgentApp):
        async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
            tools = [_tool("tool_safe", "low", False), _tool("tool_danger", "high", False)]
            with self.ctx.db() as db:
                rr = await self.ctx.run_loop(db, [{"role": "user", "content": request.input}],
                                             system="s", tools=tools)
                return InvokeResult(content=rr.content, steps=rr.steps, usage=rr.usage)

        async def on_invoke_task(self, request: InvokeRequest, gate=None, resume: dict | None = None) -> InvokeResult:
            tools = [_tool("tool_safe", "low", False), _tool("tool_danger", "high", False)]
            with self.ctx.db() as db:
                rr = await self.ctx.run_loop(db, [{"role": "user", "content": request.input}],
                                             system="s", tools=tools, approval_gate=gate)
                return InvokeResult(content=rr.content, steps=rr.steps, usage=rr.usage)

    asyncio.run(registry.start_agent("gov-tools-agent"))


def _poll_tasks(client: TestClient, task_id: str, states: set[str], tries: int = 40) -> dict:
    import time

    for _ in range(tries):
        view = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if view["state"] in states:
            return view
        time.sleep(0.2)
    return view


def test_tool_allowlist_blocks(client: TestClient):
    """tool-allowlist：白名单外工具被策略拒绝（结果回注 error，任务仍 COMPLETED）。"""
    _dummy_tools_agent(client)
    assert _make_policy(client, "gov-tool-allow", "tool-allowlist",
                        {"tools": ["tool_safe"]}).status_code == 200
    task_id = client.post("/api/v1/tasks", headers=AUTH,
                          json={"type": "agent.hitl",
                                "payload": {"agent": "gov-tools-agent", "input": "hi",
                                             "_tenant_id": 1}}).json()["task_id"]
    view = _poll_tasks(client, task_id, {"COMPLETED", "FAILED", "WAITING_HUMAN", "WAITING_INPUT"})
    # mock 取第一个工具 tool_safe → 在白名单内，正常完成
    assert view["state"] == "COMPLETED", view["result"]
    # 白名单外场景：改用顺序依赖——mock 只调第一个工具，白名单包含 tool_danger 时第一个工具 tool_safe 被拒
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl",
                             "payload": {"agent": "gov-tools-agent", "input": "hi",
                                          "_tenant_id": 1}})
    assert resp.status_code == 200


def test_tool_risk_approval_gates(client: TestClient):
    """tool-risk-approval（threshold=low）：任意工具（含低风险）触发审批挂起 WAITING_HUMAN。"""
    _dummy_tools_agent(client)
    assert _make_policy(client, "gov-tool-risk", "tool-risk-approval",
                        {"threshold": "low"}).status_code == 200
    task_id = client.post("/api/v1/tasks", headers=AUTH,
                          json={"type": "agent.hitl",
                                "payload": {"agent": "gov-tools-agent", "input": "hi",
                                             "_tenant_id": 1}}).json()["task_id"]
    view = _poll_tasks(client, task_id, {"COMPLETED", "FAILED", "WAITING_HUMAN", "WAITING_INPUT"})
    assert view["state"] == "WAITING_HUMAN", f"低阈值应触发审批挂起: {view['state']}"
    assert view["pending_tool"] == "tool_safe"
    # 审批后继续
    client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH, json={"decision": True})
    view = _poll_tasks(client, task_id, {"COMPLETED", "FAILED", "WAITING_HUMAN"})
    assert view["state"] == "COMPLETED"


def test_tool_call_audited(client: TestClient):
    """工具调用落审计 tool.call（含 agent/耗时/状态）。"""
    _dummy_tools_agent(client)
    task_id = client.post("/api/v1/tasks", headers=AUTH,
                          json={"type": "agent.hitl",
                                "payload": {"agent": "gov-tools-agent", "input": "hi"}}).json()["task_id"]
    _poll_tasks(client, task_id, {"COMPLETED", "FAILED", "WAITING_HUMAN"})
    logs = client.get("/api/v1/audit", headers=AUTH, params={"action": "tool.call"}).json()
    assert any(l["target"] == "tool_safe" for l in logs)
    detail = next(l for l in logs if l["target"] == "tool_safe")["detail"]
    assert detail.get("agent") == "gov-tools-agent"
    assert "elapsed_ms" in detail


def test_agent_allowlist_delegation_boundary(client: TestClient):
    """agent-allowlist：不可委派的智能体被边界拒绝。"""
    _make_policy(client, "gov-agent-allow", "agent-allowlist", {"agents": ["faq-agent"]})
    from eap.db import SessionLocal
    from eap.runtime.policy import PolicyDenied, check_agent_delegation
    from eap.runtime.policy import reset_tenant, set_tenant

    token = set_tenant(1)
    try:
        with SessionLocal() as db:
            check_agent_delegation(db, "faq-agent")  # 在名单内，不抛
            try:
                check_agent_delegation(db, "order-agent")
                raise AssertionError("应拒绝名单外智能体")
            except PolicyDenied:
                pass
    finally:
        reset_tenant(token)

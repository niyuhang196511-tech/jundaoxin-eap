"""Workflow DSL 引擎测试：创建即注册 / llm+检索节点 / 条件分支 / 工具节点 / 停用。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_workflow_create_register_invoke(client: TestClient):
    """创建 DSL 工作流 → 立即成为智能体 → 调用带步骤轨迹与引用。"""
    dsl = {
        "name": "triage-flow",
        "version": "1.0.0",
        "description": "客服分流工作流",
        "steps": [
            {"id": "retrieve", "type": "retrieve", "kb": "website-faq", "top_k": 2},
            {"id": "answer", "type": "llm", "knowledge": ["website-faq"],
             "system": "你是客服，依据资料回答。"},
        ],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == 200, resp.text
    assert resp.json()["invoke"].startswith("/api/v1/agents/triage-flow")

    # 出现在智能体目录
    names = [a["name"] for a in client.get("/api/v1/agents", headers=AUTH).json()]
    assert "triage-flow" in names

    # 调用
    resp = client.post("/api/v1/agents/triage-flow/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "retrieve" in " ".join(data["steps"])
    assert any(c["kb"] == "website-faq" for c in data["citations"])


def test_workflow_branch_condition(client: TestClient):
    """条件分支：包含"投诉"走升级分支（工具节点），否则走普通回答。"""
    dsl = {
        "name": "complaint-flow",
        "version": "1.0.0",
        "steps": [
            {"id": "check", "type": "branch",
             "left": "$input", "op": "contains", "right": "投诉",
             "then_id": "escalate", "else_id": "answer"},
            {"id": "escalate", "type": "tool", "tool_name": "test.escalate"},
            {"id": "answer", "type": "llm", "system": "安抚用户并说明处理流程。"},
        ],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == 200

    # 注册演示升级工具
    from eap.runtime.workflow import register_workflow_tool

    async def escalate_handler(args: str) -> str:
        return '{"ticket": "TK-001", "status": "escalated"}'

    register_workflow_tool("test.escalate", lambda: __import__(
        "eap.runtime.tools", fromlist=["Tool"]).Tool(
        name="test.escalate", description="升级工单",
        parameters={"type": "object", "properties": {}},
        handler=escalate_handler))

    # 走 then 分支（工具执行）
    resp = client.post("/api/v1/agents/complaint-flow/invocations", headers=AUTH,
                       json={"input": "我要投诉这个产品"})
    assert resp.status_code == 200
    data = resp.json()
    assert any("escalate" in s for s in data["steps"])

    # 走 else 分支
    resp = client.post("/api/v1/agents/complaint-flow/invocations", headers=AUTH,
                       json={"input": "产品怎么使用"})
    data = resp.json()
    assert any("answer" in s for s in data["steps"])


def test_workflow_when_skip(client: TestClient):
    """步骤级 when：条件不满足则跳过该步骤。"""
    dsl = {
        "name": "skip-flow",
        "version": "1.0.0",
        "steps": [
            {"id": "maybe", "type": "llm", "system": "测试",
             "when": {"left": "$input", "op": "contains", "right": "触发"}},
            {"id": "final", "type": "llm", "system": "总结"},
        ],
    }
    assert client.post("/api/v1/workflows", headers=AUTH, json=dsl).status_code == 200

    resp = client.post("/api/v1/agents/skip-flow/invocations", headers=AUTH,
                       json={"input": "不包含关键词"})
    assert resp.status_code == 200
    assert any("skipped" in s for s in resp.json()["steps"])


def test_workflow_disable(client: TestClient):
    assert client.post("/api/v1/workflows", headers=AUTH, json={
        "name": "doomed-flow", "version": "1.0.0",
        "steps": [{"id": "s", "type": "llm", "system": "x"}],
    }).status_code == 200
    resp = client.delete("/api/v1/workflows/doomed-flow", headers=AUTH)
    assert resp.status_code == 200
    names = [a["name"] for a in client.get("/api/v1/agents", headers=AUTH).json()]
    assert "doomed-flow" not in names

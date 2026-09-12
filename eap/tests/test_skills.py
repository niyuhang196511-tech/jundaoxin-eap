"""技能中心测试：CRUD / 渐进披露 / Agent 注入。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_skill_crud_and_progressive_disclosure(client: TestClient):
    # 种子技能在 L1 目录只暴露 name+description
    resp = client.get("/api/v1/skills", headers=AUTH)
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()]
    assert "customer-service" in names
    seeded = next(s for s in resp.json() if s["name"] == "customer-service")
    assert "instructions" not in seeded, "L1 目录不得携带全文（渐进披露）"

    # L2 详情：完整指令
    resp = client.get("/api/v1/skills/customer-service", headers=AUTH)
    assert resp.status_code == 200
    assert "工单" in resp.json()["instructions"]

    # 新建 + 重名 409
    assert client.post("/api/v1/skills", headers=AUTH, json={
        "name": "excel-report", "description": "周报生成",
        "instructions": "生成带图表的 Excel 周报。"}).status_code == 200
    assert client.post("/api/v1/skills", headers=AUTH, json={
        "name": "excel-report", "instructions": "dup"}).status_code == 409

    # 停用后 skill_context 不再注入
    assert client.patch("/api/v1/skills/excel-report", params={"enabled": False},
                        headers=AUTH).status_code == 200


def test_skill_injected_into_agent(client: TestClient):
    """faq-agent 声明 customer-service 技能 → 运行时加载 L2 指令进上下文。"""
    from eap.agents.registry import registry

    agent = registry.get("faq-agent")
    assert "customer-service" in agent.manifest.skills

    text = agent.instance.ctx.skill_context(["customer-service"])
    assert "工单" in text
    # 停用后不再注入
    client.patch("/api/v1/skills/customer-service", params={"enabled": False}, headers=AUTH)
    assert agent.instance.ctx.skill_context(["customer-service"]) == ""
    client.patch("/api/v1/skills/customer-service", params={"enabled": True}, headers=AUTH)

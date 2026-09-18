"""交互引擎测试（v0.5-③④⑧）：UI Schema 校验 / 聊天+任务通道挂起恢复 / 动态选项。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from .conftest import AUTH

FORM = {
    "type": "form",
    "title": "补货信息",
    "fields": [
        {"type": "select", "id": "product", "label": "产品", "required": True,
         "data_source": {"type": "tool", "tool": "erp.inventory.query",
                          "args": {}, "label_field": "product", "value_field": "product"}},
        {"type": "number", "id": "qty", "label": "数量", "required": True},
    ],
}


def test_schema_validation():
    """UI Schema 白名单校验：非法控件/嵌套 form/野键拒绝。"""
    from eap.runtime.interaction import validate_interaction_schema

    validate_interaction_schema(FORM)  # 不抛即合规
    import pytest

    with pytest.raises(ValueError, match="控件类型"):
        validate_interaction_schema({"type": "form", "fields": [{"type": "date", "id": "d"}]})
    with pytest.raises(ValueError, match="内嵌 form"):
        validate_interaction_schema({"type": "form", "fields": [
            {"type": "form", "fields": [{"type": "text", "id": "x"}]}]})
    with pytest.raises(ValueError, match="不支持 data_source"):
        validate_interaction_schema({"type": "form", "fields": [
            {"type": "text", "id": "x", "data_source": {"type": "tool", "tool": "t"}}]})


def test_chat_channel_interaction_roundtrip(client: TestClient):
    """聊天通道：invoke 挂起 → result.interaction → submit → 恢复流。"""
    resp = client.post("/api/v1/agents/warehouse-agent/invocations", headers=AUTH,
                       json={"input": "补货", "session_id": "itx-e2e-1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    interaction = data.get("interaction")
    assert interaction is not None, "应挂起等待交互"
    assert interaction["id"].startswith("itx-")
    assert interaction["key"] == "restock"
    fields = {f["id"]: f for f in interaction["ui_schema"]["fields"]}
    # 动态选项已服务端解析（mock-erp 库存查询）
    product = fields["product"]
    assert product["options"], "动态选项应已解析"
    assert product["options"][0]["label"]

    # 重复提交同一交互 → 409
    resp2 = client.post(f"/api/v1/agents/warehouse-agent/interactions/{interaction['id']}/submit",
                        headers=AUTH, json={"values": {"product": "EAP 一体机", "qty": 5}})
    assert resp2.status_code == 200, resp2.text
    # SSE 恢复流：最终 result.content 含提交值
    body = resp2.text
    result_frames = [json.loads(block.split("data: ", 1)[1])
                     for block in body.split("\n\n") if block.startswith("event: result")]
    assert result_frames and "EAP 一体机" in result_frames[0]["content"]

    # 已提交的交互再提交 → 409
    resp3 = client.post(f"/api/v1/agents/warehouse-agent/interactions/{interaction['id']}/submit",
                        headers=AUTH, json={"values": {"product": "x", "qty": 1}})
    assert resp3.status_code == 409


def test_task_channel_interaction_roundtrip(client: TestClient):
    """任务通道：agent.hitl 提交 → WAITING_INPUT → interact → 续跑 COMPLETED。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl", "payload": {"agent": "warehouse-agent",
                                                               "input": "补货"}})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    # 轮询等挂起
    import time

    state = ""
    for _ in range(40):
        view = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        state = view["state"]
        if state in ("WAITING_INPUT", "COMPLETED", "FAILED"):
            break
        time.sleep(0.2)
    assert state == "WAITING_INPUT", f"应挂起等待交互，实际 {state}"
    assert view["pending_interaction"]["key"] == "restock"

    resp = client.post(f"/api/v1/tasks/{task_id}/interact", headers=AUTH,
                       json={"values": {"product": "EAP 网关", "qty": 3}})
    assert resp.status_code == 200, resp.text
    for _ in range(40):
        view = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if view["state"] == "COMPLETED":
            break
        time.sleep(0.2)
    assert view["state"] == "COMPLETED"
    assert "EAP 网关" in view["result"]["output"]


def test_interaction_options_cascade_endpoint(client: TestClient):
    """级联选项端点：按父字段值实时重取 options（审计落库）。"""
    # 先造一个挂起交互
    resp = client.post("/api/v1/agents/warehouse-agent/invocations", headers=AUTH,
                       json={"input": "补货", "session_id": "itx-e2e-2"})
    interaction = resp.json()["interaction"]
    # product 字段有 data_source → options 端点可重取
    resp = client.post(f"/api/v1/agents/warehouse-agent/interactions/{interaction['id']}/options",
                       headers=AUTH, json={"field": "product", "values": {}})
    assert resp.status_code == 200, resp.text
    options = resp.json()["options"]
    assert isinstance(options, list)
    # 无数据源字段 → 400
    resp = client.post(f"/api/v1/agents/warehouse-agent/interactions/{interaction['id']}/options",
                       headers=AUTH, json={"field": "qty", "values": {}})
    assert resp.status_code == 400


def test_options_recorded_in_audit(client: TestClient):
    """动态选项调用落审计（interaction.options）。"""
    logs = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "interaction.options"}).json()
    assert logs, "动态选项解析应落审计"

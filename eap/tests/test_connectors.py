"""企业连接器：登记 / 验证 / 启停 / 工具注入 / order-agent 走连接器 ERP（docs/04 §4）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
MOCK_ERP_TOOLS = {"erp.inventory.query", "erp.order.create"}


def test_seeded_mock_erp_and_tool_schema(client):
    # 种子连接器已登记并验证
    names = {c["name"]: c for c in client.get("/api/v1/connectors", headers=HEADERS).json()}
    assert "mock-erp" in names
    assert names["mock-erp"]["status"] == "verified"
    assert set(names["mock-erp"]["endpoints"]) == MOCK_ERP_TOOLS

    # /tools 输出 OpenAI function-calling Schema；下单工具带审批标记
    schemas = client.get("/api/v1/connectors/mock-erp/tools", headers=HEADERS).json()
    fn_names = {s["function"]["name"] for s in schemas}
    assert fn_names == MOCK_ERP_TOOLS


def test_rest_connector_register_validate_toggle(client, uname):
    # rest 连接器必须给 http(s) base_url
    r = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": "bad-erp", "kind": "rest", "base_url": "ftp://x",
        "endpoints": [{"name": "ping", "tool_name": "bad.ping"}]})
    assert r.status_code == 400

    # 登记一个不可达的 rest 连接器 → 验证为 unreachable（M52-D：唯一名可重入；
    # 种子 mock-erp 幂等不受影响，其成员断言保持固定名）
    name = uname("corp-erp")
    r = client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": name, "kind": "rest", "base_url": "http://127.0.0.1:9",
        "description": "企业 ERP（演示用不可达地址）",
        "endpoints": [{"name": "order.create", "tool_name": "erp2.order.create",
                       "method": "POST", "path": "/orders"}]})
    assert r.status_code == 200
    r = client.post(f"/api/v1/connectors/{name}/validate", headers=HEADERS)
    assert r.json()["status"] == "unreachable"

    # 停用后不再注入工具；重名 409（同一唯一名重复提交）
    assert client.post(f"/api/v1/connectors/{name}/enabled?enabled=false",
                       headers=HEADERS).json()["enabled"] is False
    assert client.post("/api/v1/connectors", headers=HEADERS, json={
        "name": name, "kind": "rest", "base_url": "http://127.0.0.1:9",
        "endpoints": [{"name": "ping", "tool_name": "erp2.ping"}]}).status_code == 409
    # 未启用连接器 tools 为空
    assert client.get(f"/api/v1/connectors/{name}/tools", headers=HEADERS).json() == []


def test_order_agent_uses_connector_erp(client):
    """order-agent 优先用连接器工具：库存查询经 erp.inventory.query，下单仍触发 HITL 审批。"""
    # 直连调用：mock 模型调用第一个工具（库存查询）→ 回答含连接器 ERP 数据
    r = client.post("/api/v1/agents/order-agent/invocations", headers=HEADERS,
                    json={"input": "EAP 一体机还有库存吗？"})
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert any("erp.inventory.query" in str(s) for s in steps), steps

    # HITL：连接器下单工具 requires_approval=True，与内置工具行为一致
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.hitl",
                             "payload": {"agent": "order-agent", "input": "帮我下一台 EAP 一体机"}})
    task_id = resp.json()["task_id"]
    task = _poll(client, task_id, {"WAITING_HUMAN", "COMPLETED", "FAILED"})
    assert task["state"] == "WAITING_HUMAN", task["result"]
    assert task["pending_tool"] == "erp.order.create"

    resp = client.post(f"/api/v1/tasks/{task_id}/approve", headers=AUTH,
                       json={"decision": True, "comment": "同意"})
    assert resp.status_code == 200
    task = _poll(client, task_id, {"COMPLETED", "FAILED"})
    assert task["state"] == "COMPLETED", task["result"]
    assert "SO-2026" in str(task["result"])


def _poll(client, task_id, until_states, rounds=50):
    import time

    for _ in range(rounds):
        task = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if task["state"] in until_states:
            return task
        time.sleep(0.1)
    return task

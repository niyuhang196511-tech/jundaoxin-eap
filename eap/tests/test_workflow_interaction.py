"""工作流交互节点测试（v0.5-⑤）：挂起 → 提交 → 续跑；聊天通道统一。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from .conftest import AUTH

DSL = {
    "name": "restock-flow",
    "version": "1.0.0",
    "description": "补货流程（交互节点演示）",
    "steps": [
        {"id": "ask", "type": "interaction", "title": "补货信息",
         "ui_schema": {"type": "form", "title": "补货信息", "fields": [
             {"type": "select", "id": "product", "label": "产品", "required": True,
              "options": [{"label": "EAP 一体机", "value": "EAP 一体机"},
                          {"label": "EAP 网关", "value": "EAP 网关"}]},
             {"type": "number", "id": "qty", "label": "数量", "required": True},
         ]},
         "input_var": "restock"},
        {"id": "reply", "type": "llm", "system": "你是补货助手",
         "prompt_name": None, "query_var": "input"},
    ],
}


def _make_workflow(client: TestClient):
    resp = client.post("/api/v1/workflows", headers=AUTH, json=DSL)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_workflow_interaction_linear(client: TestClient):
    """线性 DSL：interaction 节点挂起 → run submit → 续跑 succeeded。"""
    _make_workflow(client)
    run_id = client.post("/api/v1/workflows/restock-flow/test-run", headers=AUTH,
                         json={"input": "开始"}).json()["run_id"]
    status = ""
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        status = view["status"]
        if status != "running":
            break
        time.sleep(0.2)
    assert status == "waiting_input", f"应在交互节点挂起，实际 {status}"
    assert view["pending_interaction"]["ui_schema"]["title"] == "补货信息"

    resp = client.post(f"/api/v1/workflows/runs/{run_id}/submit", headers=AUTH,
                       json={"values": {"product": "EAP 一体机", "qty": 5}})
    assert resp.status_code == 200, resp.text
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] == "succeeded":
            break
        time.sleep(0.2)
    assert view["status"] == "succeeded", f"应续跑完成，实际 {view['status']}: {view.get('error')}"


def test_workflow_interaction_graph_mode(client: TestClient):
    """图 DSL（edges）：interaction 节点同样可挂起/恢复。"""
    graph = dict(DSL, name="restock-graph", steps=[
        {"id": "start", "type": "tool", "tool_name": "kb.product-docs.search",
         "tool_args": {"query": "x"}},
        DSL["steps"][0],
        DSL["steps"][1],
    ], edges=[
        {"id": "e1", "source": "start", "target": "ask"},
        {"id": "e2", "source": "ask", "target": "reply"},
    ])
    resp = client.post("/api/v1/workflows", headers=AUTH, json=graph)
    assert resp.status_code == 200, resp.text
    run_id = client.post("/api/v1/workflows/restock-graph/test-run", headers=AUTH,
                         json={"input": "开始"}).json()["run_id"]
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            break
        time.sleep(0.2)
    assert view["status"] == "waiting_input"
    resp = client.post(f"/api/v1/workflows/runs/{run_id}/submit", headers=AUTH,
                       json={"values": {"product": "EAP 网关", "qty": 2}})
    assert resp.status_code == 200
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] == "succeeded":
            break
        time.sleep(0.2)
    assert view["status"] == "succeeded"


def test_workflow_agent_chat_interaction(client: TestClient):
    """workflow-as-agent 聊天通道：挂起返回 result.interaction → submit 恢复协议续跑。"""
    chat_dsl = dict(DSL, name="restock-chat")
    resp = client.post("/api/v1/workflows", headers=AUTH, json=chat_dsl)
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/v1/agents/restock-chat/invocations", headers=AUTH,
                       json={"input": "补货", "session_id": "wf-itx-1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    interaction = data.get("interaction")
    assert interaction is not None, "工作流 agent 应挂起等待交互"
    assert interaction["id"].startswith("itx-")

    resp = client.post(f"/api/v1/agents/restock-chat/interactions/{interaction['id']}/submit",
                       headers=AUTH, json={"values": {"product": "EAP 一体机", "qty": 5},
                                            "session_id": "wf-itx-1"})
    assert resp.status_code == 200, resp.text
    result_frames = [json.loads(block.split("data: ", 1)[1])
                     for block in resp.text.split("\n\n") if block.startswith("event: result")]
    assert result_frames, "恢复流应有 result 帧"
    assert result_frames[0].get("interaction") is None


def test_workflow_submit_non_waiting_409(client: TestClient):
    run_id = client.post("/api/v1/workflows/restock-flow/test-run", headers=AUTH,
                         json={"input": "开始"}).json()["run_id"]
    # 立即 submit：可能还在 running（未挂起）或已 waiting_input；等待挂起后再提交两次
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            break
        time.sleep(0.2)
    assert view["status"] == "waiting_input"
    resp = client.post(f"/api/v1/workflows/runs/{run_id}/submit", headers=AUTH,
                       json={"values": {"product": "x", "qty": 1}})
    assert resp.status_code == 200
    # 等待续跑完成后再提交 → 409
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            break
        time.sleep(0.2)
    resp2 = client.post(f"/api/v1/workflows/runs/{run_id}/submit", headers=AUTH,
                        json={"values": {"product": "y", "qty": 2}})
    assert resp2.status_code == 409

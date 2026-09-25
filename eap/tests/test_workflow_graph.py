"""Workflow DSL v2 测试：图遍历执行 / branch 出口边 / loop 节点 / 旧线性 DSL 兼容 / 运行记录。

M52-D 可重入：落库的工作流名 uname 唯一化（脏库重跑不撞唯一约束）；
仅 422 拒绝（不落库）的坏 DSL 名保持固定。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import AUTH


def _make_graph_dsl(name: str) -> dict:
    """检索 → 分支（含"投诉"走升级）→ 汇聚节点。"""
    return {
        "name": name,
        "version": "1.0.0",
        "description": "图形态工作流",
        "steps": [
            {"id": "start", "type": "retrieve", "kb": "website-faq", "top_k": 2,
             "title": "开始检索", "position": {"x": 80, "y": 120}},
            {"id": "route", "type": "branch",
             "left": "$input", "op": "contains", "right": "投诉",
             "title": "智能分流", "position": {"x": 320, "y": 120}},
            {"id": "escalate", "type": "tool", "tool_name": "test.escalate",
             "position": {"x": 560, "y": 40}},
            {"id": "answer", "type": "llm", "system": "安抚用户并说明处理流程。",
             "position": {"x": 560, "y": 200}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "route"},
            {"id": "e2", "source": "route", "target": "escalate", "source_handle": "then"},
            {"id": "e3", "source": "route", "target": "answer", "source_handle": "else"},
        ],
    }


def test_workflow_graph_executes_and_runs_recorded(client: TestClient, uname):
    """图 DSL：创建即注册 → 调用走图遍历 → test-run 逐节点运行记录落库。"""
    from eap.runtime.workflow import register_workflow_tool

    async def escalate_handler(args: str) -> str:
        return '{"ticket": "TK-100", "status": "escalated"}'

    register_workflow_tool("test.escalate", lambda: __import__(
        "eap.runtime.tools", fromlist=["Tool"]).Tool(
        name="test.escalate", description="升级工单",
        parameters={"type": "object", "properties": {}}, handler=escalate_handler))

    name = uname("graph-flow")
    dsl = _make_graph_dsl(name)
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"] == 4

    # 图遍历调用：不含"投诉" → else → answer
    resp = client.post(f"/api/v1/agents/{name}/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any("route(branch): else" in s for s in data["steps"]), data["steps"]
    assert "TK-100" not in data["output"]

    # 含"投诉" → then → escalate
    resp = client.post(f"/api/v1/agents/{name}/invocations", headers=AUTH,
                       json={"input": "我要投诉！"})
    assert resp.status_code == 200, resp.text
    assert "TK-100" in resp.json()["output"]

    # 试运行：run_id → 轮询到终态 → 节点运行记录完整
    resp = client.post(f"/api/v1/workflows/{name}/test-run", headers=AUTH,
                       json={"input": "投诉测试"})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    detail = None
    for _ in range(50):
        detail = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if detail["status"] != "running":
            break
        time.sleep(0.2)
    assert detail is not None and detail["status"] == "succeeded", detail
    node_ids = [n["id"] for n in detail["node_runs"]]
    assert node_ids == ["start", "route", "escalate"], node_ids
    escalate_run = detail["node_runs"][2]
    assert escalate_run["status"] == "ok" and "TK-100" in escalate_run["output"]
    assert detail["elapsed_ms"] >= 0

    # 历史列表
    runs = client.get(f"/api/v1/workflows/{name}/runs", headers=AUTH).json()
    assert any(r["id"] == run_id for r in runs)


def test_workflow_graph_validation(client: TestClient):
    """边引用不存在节点 / 重复边 → DSL 校验拒绝（pydantic 层 422）。"""
    bad = _make_graph_dsl("graph-bad-edge")
    bad["edges"].append({"id": "e9", "source": "ghost", "target": "answer"})
    resp = client.post("/api/v1/workflows", headers=AUTH, json=bad)
    assert resp.status_code == 422
    assert "ghost" in resp.text

    dup = _make_graph_dsl("graph-bad-dup")
    dup["edges"].append({"id": "e-dup", "source": "start", "target": "route"})
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dup)
    assert resp.status_code == 422


def test_workflow_loop_node(client: TestClient, uname):
    """loop 节点：对数组变量逐项执行 body，拼接输出并保存 items。"""
    from eap.runtime.workflow import register_workflow_tool

    async def array_handler(args: str) -> str:
        return '["北京", "上海", "深圳"]'

    register_workflow_tool("test.array", lambda: __import__(
        "eap.runtime.tools", fromlist=["Tool"]).Tool(
        name="test.array", description="返回城市数组",
        parameters={"type": "object", "properties": {}}, handler=array_handler))

    name = uname("loop-flow")
    dsl = {
        "name": name,
        "version": "1.0.0",
        "steps": [
            {"id": "cities", "type": "tool", "tool_name": "test.array"},
            {"id": "each", "type": "loop", "loop_var": "$cities", "item_var": "city",
             "max_iterations": 10, "join_with": "|",
             "body": [{"id": "wrap", "type": "llm", "system": "城市：$city"}]},
            {"id": "final", "type": "llm", "system": "汇总结果：$each"},
        ],
        "edges": [
            {"id": "e1", "source": "cities", "target": "each"},
            {"id": "e2", "source": "each", "target": "final"},
        ],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/agents/{name}/invocations", headers=AUTH,
                       json={"input": "开始"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # mock llm 确定性回声：每轮 body 输出含迭代变量值，拼接符 | 生效
    assert any("loop): 3/3 items" in s for s in data["steps"]), data["steps"]


def test_workflow_loop_validation(client: TestClient):
    """loop 缺 loop_var / body 含非法节点类型 → 409。"""
    bad = {
        "name": "loop-bad",
        "version": "1.0.0",
        "steps": [{"id": "lp", "type": "loop",
                   "body": [{"id": "inner", "type": "llm"}]}],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=bad)
    assert resp.status_code == 422  # pydantic 校验失败

    bad2 = {
        "name": "loop-bad2",
        "version": "1.0.0",
        "steps": [{"id": "lp", "type": "loop", "loop_var": "$x",
                   "body": [{"id": "inner", "type": "subflow", "workflow": "other"}]}],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=bad2)
    assert resp.status_code == 422


def test_workflow_linear_dsl_still_works(client: TestClient, uname):
    """旧线性 DSL（无 edges）走原执行路径不回归。"""
    name = uname("legacy-flow")
    dsl = {
        "name": name,
        "version": "1.0.0",
        "steps": [
            {"id": "a", "type": "llm", "system": "第一步"},
            {"id": "b", "type": "llm", "system": "第二步"},
        ],
    }
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == 200

    resp = client.post(f"/api/v1/agents/{name}/invocations", headers=AUTH,
                       json={"input": "hello"})
    assert resp.status_code == 200, resp.text
    steps = resp.json()["steps"]
    assert any("a(llm" in s for s in steps) and any("b(llm" in s for s in steps)

    # dsl 接口返回全文（无 edges 字段 → 前端 linear_to_edges 兜底可视化）
    dsl_full = client.get(f"/api/v1/workflows/{name}/dsl", headers=AUTH).json()
    assert "edges" not in dsl_full or dsl_full["edges"] == []

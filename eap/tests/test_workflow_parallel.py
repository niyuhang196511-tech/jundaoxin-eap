"""Workflow 并行节点与子流程：并发分支 / subflow 委派 / 深度护栏防环（docs/03 §6）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def _create(client, dsl: dict, expect=200):
    resp = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert resp.status_code == expect, resp.text
    return resp.json()


def test_parallel_branches(client: TestClient):
    """并行节点：两分支并发（llm + retrieve），输出按 join_with 拼接、引用合并。"""
    dsl = {
        "name": "parallel-flow",
        "version": "1.0.0",
        "description": "并行：检索 + 独立 LLM 汇总",
        "steps": [
            {"id": "fanout", "type": "parallel", "join_with": " || ", "branches": [
                {"id": "search", "steps": [
                    {"id": "r1", "type": "retrieve", "kb": "website-faq", "top_k": 2}]},
                {"id": "think", "steps": [
                    {"id": "l1", "type": "llm", "system": "你是分析助手，简短作答。"}]},
            ]},
            {"id": "final", "type": "llm", "system": "汇总以下材料作答：$fanout"},
        ],
    }
    _create(client, dsl)
    resp = client.post("/api/v1/agents/parallel-flow/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 并行步骤轨迹（两分支都有）+ 引用来自检索分支
    assert any("fanout/" in s and "search:r1" in s for s in data["steps"]), data["steps"]
    assert any("fanout/" in s and "think:l1" in s for s in data["steps"])
    assert any(c["kb"] == "website-faq" for c in data["citations"])
    # 汇总步骤引用 $fanout（拼接输出作为 system 一部分进了 mock 回声？——mock 只回声 user
    # 输入，但 final 步骤本身必须执行成功并产出非空 output
    assert data["output"]


def test_parallel_rejects_invalid_inner_step(client: TestClient):
    """并行分支内不允许 branch/parallel/subflow（确定性重放保护）。"""
    dsl = {
        "name": "bad-parallel",
        "version": "1.0.0",
        "steps": [
            {"id": "p", "type": "parallel", "branches": [
                {"id": "a", "steps": [{"id": "inner", "type": "branch",
                                       "op": "eq", "left": "x", "right": "y"}]}]},
        ],
    }
    r = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert r.status_code == 422  # pydantic 在引擎校验时抛错 → API 500？改为显式 422 由模型校验
    names = [a["name"] for a in client.get("/api/v1/agents", headers=AUTH).json()]
    assert "bad-parallel" not in names


def test_subflow_delegation_and_cycle_guard(client: TestClient):
    """subflow：主流程调用子工作流（含引用合并）；循环引用触发深度护栏不崩溃。"""
    _create(client, {
        "name": "sub-faq",
        "version": "1.0.0",
        "steps": [
            {"id": "r", "type": "retrieve", "kb": "website-faq", "top_k": 2},
            {"id": "a", "type": "llm", "knowledge": ["website-faq"], "system": "子流程回答。"},
        ],
    })
    _create(client, {
        "name": "main-flow",
        "version": "1.0.0",
        "steps": [
            {"id": "sub", "type": "subflow", "workflow": "sub-faq"},
            {"id": "wrap", "type": "llm", "system": "包装子流程结果：$sub"},
        ],
    })
    resp = client.post("/api/v1/agents/main-flow/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any("subflow" in s and "sub-faq" in s for s in data["steps"]), data["steps"]
    assert any(c["kb"] == "website-faq" for c in data["citations"])  # 子流程引用上浮

    # 循环引用：A→B→A，深度护栏生效，调用不崩溃（护栏跳过并继续）
    _create(client, {"name": "cyc-a", "version": "1.0.0",
                     "steps": [{"id": "to-b", "type": "subflow", "workflow": "cyc-b"}]})
    _create(client, {"name": "cyc-b", "version": "1.0.0",
                     "steps": [{"id": "to-a", "type": "subflow", "workflow": "cyc-a"}]})
    resp = client.post("/api/v1/agents/cyc-a/invocations", headers=AUTH,
                       json={"input": "hi"})
    assert resp.status_code == 200, resp.text  # 护栏跳过，流程正常收尾

    # subflow 目标不存在 → DSL 错误映射 503 EAP-4005
    _create(client, {"name": "dangling", "version": "1.0.0",
                     "steps": [{"id": "d", "type": "subflow", "workflow": "no-such-wf"}]})
    resp = client.post("/api/v1/agents/dangling/invocations", headers=AUTH,
                       json={"input": "hi"})
    assert resp.status_code == 503 and "EAP-4005" in resp.json()["detail"]

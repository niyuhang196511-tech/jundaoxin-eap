"""人工抽检测试（M44-B）：任务抽样 / 防重复 / 评分校验 / 报表聚合 / RBAC。

离线确定性：抽样源用 agent.invoke 任务（mock LLM 快速到 COMPLETED）。
M52-D 可重入：报表专属工作流名加模块级 uuid 后缀——脏库重跑不撞「工作流已存在」，
且报表按 agent 聚合的精确断言（total==2）天然与历史残留样本隔离。
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_oidc import fake_idp  # noqa: F401 复用模拟 IdP 夹具（fixture 再导出）

_SFX = uuid.uuid4().hex[:8]
REPORT_FLOW = f"review-report-flow-{_SFX}"


def _submit_and_wait(client: TestClient, input_text: str) -> dict:
    """提交 agent.invoke 任务并轮询到 COMPLETED，返回任务视图。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "faq-agent", "input": input_text}})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    deadline = time.time() + 8
    while time.time() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if task.get("state") in ("COMPLETED", "FAILED"):
            return task
        time.sleep(0.2)
    raise AssertionError(f"任务 {task_id} 未到终态")


def _sample(client: TestClient, task_id: str):
    return client.post("/api/v1/evals/reviews/sample", headers=AUTH, json={"task_id": task_id})


def test_review_sample_and_submit_flow(client: TestClient):
    task = _submit_and_wait(client, "人工抽检输入一")

    # 抽样：快照 input/output + 归属 agent
    r = _sample(client, task["task_id"])
    assert r.status_code == 200, r.text
    sample = r.json()
    assert sample["agent"] == "faq-agent" and sample["status"] == "pending"
    assert sample["source_id"] == task["task_id"]

    # 防重复：同任务再抽 → 409
    assert _sample(client, task["task_id"]).status_code == 409

    # 列表/详情可见
    rows = client.get("/api/v1/evals/reviews", headers=AUTH,
                      params={"status": "pending"}).json()
    assert any(x["id"] == sample["id"] for x in rows)

    # 评分校验：未知维度 400 / 越界分值 400 / 空 scores 400
    sid = sample["id"]
    assert client.post(f"/api/v1/evals/reviews/{sid}/review", headers=AUTH,
                       json={"scores": {"wrong": 5}}).status_code == 400
    assert client.post(f"/api/v1/evals/reviews/{sid}/review", headers=AUTH,
                       json={"scores": {"correctness": 9}}).status_code == 400
    assert client.post(f"/api/v1/evals/reviews/{sid}/review", headers=AUTH,
                       json={"scores": {}}).status_code == 400

    # 正常评审：pending → reviewed
    r = client.post(f"/api/v1/evals/reviews/{sid}/review", headers=AUTH,
                    json={"scores": {"correctness": 5, "relevance": 4, "format": 4},
                          "note": "回答正确，格式略简"})
    assert r.status_code == 200 and r.json()["status"] == "reviewed"

    # 已评审再提交 → 409
    assert client.post(f"/api/v1/evals/reviews/{sid}/review", headers=AUTH,
                       json={"scores": {"correctness": 3}}).status_code == 409


def test_review_report_aggregates(client: TestClient):
    # 专属 agent：报表按 agent 聚合，与其他用例的 faq-agent 样本互不污染
    dsl = {"name": REPORT_FLOW, "version": "1.0.0",
           "steps": [{"id": "gen", "type": "llm", "system": "你是测试助手。"}]}
    assert client.post("/api/v1/workflows", headers=AUTH, json=dsl).status_code == 200

    def _submit(input_text: str) -> dict:
        resp = client.post("/api/v1/tasks", headers=AUTH,
                           json={"type": "agent.invoke",
                                 "payload": {"agent": REPORT_FLOW, "input": input_text}})
        assert resp.status_code == 200
        return resp.json()

    t1 = _submit("报表抽检输入一")
    t2 = _submit("报表抽检输入二")
    s1 = _sample(client, t1["task_id"]).json()
    s2 = _sample(client, t2["task_id"]).json()
    client.post(f"/api/v1/evals/reviews/{s1['id']}/review", headers=AUTH,
                json={"scores": {"correctness": 5, "relevance": 5, "format": 3}})
    client.post(f"/api/v1/evals/reviews/{s2['id']}/review", headers=AUTH,
                json={"scores": {"correctness": 3, "relevance": 5, "format": 3}})

    rep = client.get("/api/v1/evals/reviews/report", headers=AUTH,
                     params={"agent": REPORT_FLOW}).json()
    assert rep["total"] == 2 and rep["reviewed"] == 2 and rep["pending"] == 0
    assert rep["avg_scores"]["correctness"] == 4.0  # (5+3)/2
    assert rep["avg_scores"]["format"] == 3.0
    assert rep["positive_rate"] == 0.0  # 两样本均含 <4 维度


def test_review_sample_rejects_non_agent_task(client: TestClient):
    """非 agent 类型任务不可抽样 → 400。"""
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "kb.ingest", "payload": {"kb": "product-docs"}})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    r = _sample(client, task_id)
    assert r.status_code == 400
    assert "agent.invoke" in r.json()["detail"]


def test_review_rbac_member_403(client: TestClient, fake_idp):  # noqa: F811 参数仅为激活夹具（模块级导入供 pytest 发现）
    """RBAC：member JWT 抽样/评审 → 403（admin 语义）。"""
    from .test_jwt_auth import _access_token  # noqa: F401 复用 JWT 构造（需 fake_idp 配置 OIDC）

    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    assert client.post("/api/v1/evals/reviews/sample", headers=member,
                       json={"task_id": "whatever"}).status_code == 403
    assert client.post("/api/v1/evals/reviews/1/review", headers=member,
                       json={"scores": {"correctness": 5}}).status_code == 403

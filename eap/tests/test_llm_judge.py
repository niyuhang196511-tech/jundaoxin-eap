"""LLM-as-Judge 评测：模型裁判、JSON 解析、与发布门禁打通（docs/08 §3）。

离线约定：mock 模型对携带 EAP-JUDGE 标记的裁判请求返回确定性结论——
用例输入含「期望不通过」判 FAIL，否则 PASS；真实模型忽略该标记按评分标准裁判。
"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
JUDGE_DATASET = {
    "name": "llm-judge-ds",
    "cases": [
        {"input": "如何创建知识库？", "expectation": "回答需说明知识库的创建方式，语义切题"},
        {"input": "随便聊点什么 [期望不通过]", "expectation": "回答必须完整说明创建步骤"},
    ],
}




def _poll_run(client, run_id, tries=60):
    import time

    for _ in range(tries):
        body = client.get(f"/api/v1/evals/runs/{run_id}", headers=HEADERS).json()
        if body["verdict"] != "PENDING":
            return body
        time.sleep(0.2)
    return body

def test_llm_judge_run_pass_and_fail(client):
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json=JUDGE_DATASET).status_code == 200

    # 两个用例一过一不过 → 通过率 0.5：门槛 0.5 → PASS；逐用例带裁判理由
    r = client.post("/api/v1/evals/runs", headers=HEADERS,
                    json={"agent": "faq-agent", "dataset": "llm-judge-ds",
                          "min_pass_rate": 0.5, "judge": "llm"})
    body = _poll_run(client, r.json()["run_id"])
    assert body["judge"] == "llm" and body["verdict"] == "PASS" and body["pass_rate"] == 0.5
    verdicts = {s["input"]: s for s in body["scores"]}
    assert verdicts["如何创建知识库？"]["passed"] is True
    assert verdicts["随便聊点什么 [期望不通过]"]["passed"] is False
    assert verdicts["如何创建知识库？"]["judge"]["reason"]
    # 通过率记录在 run 详情
    run = client.get(f"/api/v1/evals/runs/{body['run_id']}", headers=HEADERS).json()
    assert run["judge"] == "llm" and run["verdict"] == "PASS"

    # 同一结果提高门槛到 0.8 → FAIL
    r = client.post("/api/v1/evals/runs", headers=HEADERS,
                    json={"agent": "faq-agent", "dataset": "llm-judge-ds",
                          "min_pass_rate": 0.8, "judge": "llm"})
    assert _poll_run(client, r.json()["run_id"])["verdict"] == "FAIL"


def test_llm_judge_release_gate(client):
    """发布门禁用 llm 裁判：FAIL 评测把发布挡在 review 态。"""
    # 测试间共享 session 客户端：数据集可能已由前一个用例创建
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json=JUDGE_DATASET).status_code in (200, 409)
    rel = client.post("/api/v1/releases", headers=HEADERS,
                      json={"agent": "faq-agent", "version": "20.0.0"}).json()
    client.post(f"/api/v1/releases/{rel['id']}/promote", headers=HEADERS)  # → review
    # llm 裁判 + 高门槛 → FAIL
    r = client.post(f"/api/v1/releases/{rel['id']}/eval", headers=HEADERS,
                    json={"dataset": "llm-judge-ds", "min_pass_rate": 0.9, "judge": "llm"})
    assert r.json()["eval_verdict"] == "FAIL"
    r = client.post(f"/api/v1/releases/{rel['id']}/promote", headers=HEADERS)
    assert r.status_code == 403 and "EAP-6001" in r.json()["detail"]
    # 换 PASS 的评测后放行
    r = client.post(f"/api/v1/releases/{rel['id']}/eval", headers=HEADERS,
                    json={"dataset": "llm-judge-ds", "min_pass_rate": 0.5, "judge": "llm"})
    assert r.json()["eval_verdict"] == "PASS"
    assert client.post(f"/api/v1/releases/{rel['id']}/promote", headers=HEADERS).json()["state"] == "prod"

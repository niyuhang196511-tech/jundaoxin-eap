"""RAG 评测测试（v0.6-M25）：指标纯函数 + 异步 RAG run + 回归对比。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_rag_metrics_pure_functions():
    """HitRate/Recall/MRR/NDCG 确定性计算。"""
    from eap.runtime.rag_eval import aggregate, evaluate_retrieval

    # ranked：第 2 名命中相关
    m = evaluate_retrieval([101, 205, 300], {205, 999}, k=5)
    assert m["hit_rate"] == 1.0
    assert m["recall"] == 0.5
    assert m["mrr"] == 0.5
    assert 0 < m["ndcg"] <= 1
    # 未命中
    m0 = evaluate_retrieval([1, 2, 3], {9}, k=3)
    assert m0["hit_rate"] == 0.0 and m0["mrr"] == 0.0
    # 聚合
    agg = aggregate([m, m0])
    assert agg["hit_rate"] == 0.5


def test_rag_dataset_validation(client: TestClient):
    """RAG 数据集校验：缺 relevant_chunk_ids → 400。"""
    resp = client.post("/api/v1/evals/datasets", headers=AUTH, json={
        "name": "rag-bad", "kind": "rag",
        "cases": [{"query": "如何创建知识库？"}]})
    assert resp.status_code == 400
    assert "relevant_chunk_ids" in resp.json()["detail"]


def test_rag_run_async_and_metrics(client: TestClient, uname):
    """RAG 评测全链路：标注数据集 → 异步 run → 指标落库（种子 KB 真实检索）。"""
    # 先拿 website-faq 里一个真实 chunk_id 作标注
    resp = client.post("/api/v1/kb/website-faq/retrieve", headers=AUTH,
                       json={"query": "如何创建知识库？", "top_k": 3})
    assert resp.status_code == 200, resp.text
    hits = resp.json()["hits"]
    assert hits, "种子库应有可检索内容"
    target = hits[0]["citation"]["chunk_id"]

    # M52-D：数据集唯一名——脏库重跑不撞唯一约束（409 会击穿 assert 200）
    ds_name = uname("rag-faq-smoke")
    ds = client.post("/api/v1/evals/datasets", headers=AUTH, json={
        "name": ds_name, "kind": "rag",
        "cases": [{"query": "如何创建知识库？", "relevant_chunk_ids": [target]}]})
    assert ds.status_code == 200, ds.text

    resp = client.post("/api/v1/evals/rag-runs", headers=AUTH,
                       json={"agent": "website-faq", "dataset": ds_name, "top_k": 3})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    for _ in range(50):
        run = client.get(f"/api/v1/evals/runs/{run_id}", headers=AUTH).json()
        if run["verdict"] != "PENDING":
            break
        time.sleep(0.2)
    assert run["verdict"] in ("PASS", "FAIL")
    assert run["kind"] == "rag"
    metrics = run["metrics"]
    assert set(metrics) >= {"hit_rate", "recall", "mrr", "ndcg"}
    assert metrics["hit_rate"] == 1.0, "标注了 top1 chunk，HitRate@3 应为 1"


def test_regression_comparison(client: TestClient):
    """回归对比：compare 参数输出逐指标 delta 与劣化标记。"""
    # 两个 agent 数据集 run（同数据集跑两次，pass_rate 相同 → 无劣化）
    resp = client.post("/api/v1/evals/runs", headers=AUTH,
                       json={"agent": "faq-agent", "dataset": "faq-smoke", "min_pass_rate": 0.8})
    run_id1 = resp.json()["run_id"]
    import time

    for _ in range(60):
        r1 = client.get(f"/api/v1/evals/runs/{run_id1}", headers=AUTH).json()
        if r1["verdict"] != "PENDING":
            break
        time.sleep(0.2)
    resp = client.post("/api/v1/evals/runs", headers=AUTH,
                       json={"agent": "faq-agent", "dataset": "faq-smoke", "min_pass_rate": 0.8})
    run_id2 = resp.json()["run_id"]
    for _ in range(60):
        r2 = client.get(f"/api/v1/evals/runs/{run_id2}", headers=AUTH).json()
        if r2["verdict"] != "PENDING":
            break
        time.sleep(0.2)

    detail = client.get(f"/api/v1/evals/runs/{run_id2}", headers=AUTH,
                        params={"compare": run_id1}).json()
    assert "regression" in detail
    reg = detail["regression"]
    assert reg["compare_to"] == run_id1
    metrics = {m["metric"]: m for m in reg["metrics"]}
    assert "pass_rate" in metrics
    assert metrics["pass_rate"]["regressed"] is False

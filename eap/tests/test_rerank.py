"""Reranker 两阶段检索：词面精排、llm 路径回退、默认行为不变（docs/04 §1.3）。

M52-D 可重入：KB 名 uname 唯一化——脏库重跑不撞唯一约束（409 响应无 name 键会
直接 KeyError），且每遍在全新 KB 上断言排序，免受残留文档干扰。
"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def test_lexical_rerank_improves_head_precision(client, uname):
    """构造 RRF 召回序与词面覆盖度分歧的场景：重排后覆盖度高的候选排第一。"""
    kb = client.post("/api/v1/kb", headers=HEADERS,
                     json={"name": uname("rerank-kb"), "title": "rerank-kb",
                           "template": "doc"}).json()
    name = kb["name"]
    # 文档 1：长文，关键词只出现一次（BM25/RRF 占优靠长度归一 + 多词命中分散）
    client.post(f"/api/v1/kb/{name}/documents", headers=HEADERS,
                json={"title": "长文", "text": "退货 退款 售后 保障 政策 说明 手续 流程 审核 时效。"
                                            "EAP一体机 的退货政策是三十天无理由退货。"})
    # 文档 2：短文，查询词全覆盖
    client.post(f"/api/v1/kb/{name}/documents", headers=HEADERS,
                json={"title": "短文", "text": "退货政策 三十天无理由退货。"})

    # 不重排：RRF 序
    no_rr = client.post(f"/api/v1/kb/{name}/retrieve", headers=HEADERS,
                        json={"query": "退货政策", "top_k": 2}).json()["hits"]
    # lexical 重排：全覆盖的短文必须排第一
    rr = client.post(f"/api/v1/kb/{name}/retrieve", headers=HEADERS,
                     json={"query": "退货政策", "top_k": 2, "rerank": "lexical"}).json()["hits"]
    assert len(rr) == 2
    assert rr[0]["citation"]["document"] == "短文", rr
    assert {h["citation"]["document"] for h in rr} == {h["citation"]["document"] for h in no_rr}


def test_llm_rerank_falls_back_gracefully(client, uname):
    """llm 重排：mock 回声不可解析 → 自动回退 lexical（确定性），不报错不丢结果。"""
    kb = client.post("/api/v1/kb", headers=HEADERS,
                     json={"name": uname("rerank-llm-kb"), "title": "rerank-llm-kb",
                           "template": "doc"}).json()
    name = kb["name"]
    client.post(f"/api/v1/kb/{name}/documents", headers=HEADERS,
                json={"title": "短文", "text": "退货政策 三十天无理由退货。"})
    client.post(f"/api/v1/kb/{name}/documents", headers=HEADERS,
                json={"title": "长文", "text": "退货 退款 售后 保障 政策 说明 手续 流程 审核 时效。"
                                              "EAP一体机 的退货政策是三十天无理由退货。"})
    r = client.post(f"/api/v1/kb/{name}/retrieve", headers=HEADERS,
                    json={"query": "退货政策", "top_k": 2, "rerank": "llm"}).json()["hits"]
    assert len(r) == 2
    assert r[0]["citation"]["document"] == "短文"  # 回退到 lexical，结果一致


def test_no_rerank_default_unchanged(client, uname):
    """默认不重排：不传 rerank 时行为与历史一致（排序 = 纯 RRF 融合序）。"""
    kb = client.post("/api/v1/kb", headers=HEADERS,
                     json={"name": uname("rerank-off-kb"), "title": "rerank-off-kb",
                           "template": "doc"}).json()
    name = kb["name"]
    client.post(f"/api/v1/kb/{name}/documents", headers=HEADERS,
                json={"title": "d1", "text": "知识库 创建 入口 在控制台。"})
    r1 = client.post(f"/api/v1/kb/{name}/retrieve", headers=HEADERS,
                     json={"query": "知识库", "top_k": 1}).json()["hits"]
    r2 = client.post(f"/api/v1/kb/{name}/retrieve", headers=HEADERS,
                     json={"query": "知识库", "top_k": 1, "rerank": "lexical"}).json()["hits"]
    assert r1 and r2 and r1[0]["citation"]["document"] == r2[0]["citation"]["document"]

"""RAG 评测指标（docs/unfinished v0.6-④）：HitRate@K / Recall@K / MRR / NDCG。

确定性纯函数（离线可测）：输入检索命中的 chunk_id 序列（按排名）与标注相关集合。
"""

from __future__ import annotations

import math


def hit_rate_at_k(ranked: list, relevant: set, k: int) -> float:
    """前 K 名中至少命中一个相关文档的比例（单查询=0/1）。"""
    top = ranked[:k]
    return 1.0 if any(r in relevant for r in top) else 0.0


def recall_at_k(ranked: list, relevant: set, k: int) -> float:
    """前 K 名覆盖的相关文档比例。"""
    if not relevant:
        return 0.0
    top = set(ranked[:k])
    return len(top & relevant) / len(relevant)


def mrr(ranked: list, relevant: set) -> float:
    """首个相关结果的倒数排名（MRR 的单查询分量）。"""
    for i, r in enumerate(ranked, 1):
        if r in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list, relevant: set, k: int) -> float:
    """二元相关性的 NDCG@K。"""
    if not relevant:
        return 0.0
    dcg = sum(1.0 / math.log2(i + 2) for i, r in enumerate(ranked[:k]) if r in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def evaluate_retrieval(ranked: list, relevant: list, k: int) -> dict:
    """单查询全指标。ranked=检索返回的 chunk_id 序列；relevant=标注相关集合。"""
    rel = set(relevant)
    return {
        "hit_rate": hit_rate_at_k(ranked, rel, k),
        "recall": round(recall_at_k(ranked, rel, k), 4),
        "mrr": round(mrr(ranked, rel), 4),
        "ndcg": round(ndcg_at_k(ranked, rel, k), 4),
    }


def aggregate(case_metrics: list[dict]) -> dict:
    """多查询指标均值聚合。"""
    if not case_metrics:
        return {}
    keys = case_metrics[0].keys()
    return {k: round(sum(m[k] for m in case_metrics) / len(case_metrics), 4) for k in keys}


def validate_rag_cases(cases: list[dict]) -> None:
    """RAG 数据集用例校验：query + relevant_chunk_ids。"""
    for i, case in enumerate(cases):
        if not isinstance(case, dict) or not case.get("query"):
            raise ValueError(f"RAG 用例 {i} 缺少 query")
        rel = case.get("relevant_chunk_ids")
        if not isinstance(rel, list) or not rel:
            raise ValueError(f"RAG 用例 {i} 缺少 relevant_chunk_ids（标注相关 chunk 列表）")

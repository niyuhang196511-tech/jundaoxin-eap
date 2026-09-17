"""混合检索：BM25（稀疏）+ 向量余弦（稠密）→ RRF 融合（docs/04 §1.3 的 M1 版）。

图谱路（GraphRAG/LightRAG）与重排模型在 M2 接入；生产向量路走 Milvus。
"""

from __future__ import annotations

import math


BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60


def bm25_scores(query_tokens: list[str], docs_tokens: list[list[str]]) -> list[float]:
    """经典 BM25：对每个文档打分。开发规模全量计算；生产换 ES/OpenSearch。"""
    n = len(docs_tokens)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs_tokens) / n or 1.0
    df: dict[str, int] = {}
    tfs: list[dict[str, int]] = []
    for d in docs_tokens:
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        tfs.append(tf)
        for t in tf:
            df[t] = df.get(t, 0) + 1

    scores = []
    for tf, d in zip(tfs, docs_tokens):
        score = 0.0
        for t in set(query_tokens):
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (BM25_K1 + 1) / (tf[t] + BM25_K1 * (1 - BM25_B + BM25_B * len(d) / avgdl))
        scores.append(score)
    return scores


def rrf_combine(rank_lists: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion：多路召回融合（docs/04 §1.3）。"""
    fused: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return fused


def top_n(scores: list[float], n: int) -> list[int]:
    """按分数降序返回索引（稳定：并列按原顺序）。"""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    hits = [i for i in order if scores[i] > 0]
    return hits[:n]

"""Reranker（docs/04 §1.3，检索第二阶段）：召回池 → 精排 → top_k。

- lexical：词面重排——查询词元在候选内容中的覆盖度（确定性，离线可用）
- llm：LLM listwise 重排——模型对候选逐条打相关性分（JSON 输出），
  解析/调用失败自动回退 lexical（含 mock 模型的回声输出）

两阶段语义：RRF 融合负责"召回多样性"，重排负责"头部精排"——只对召回池头部
（top_k × 3，至少 10 条）重排，不改变召回池之外的排序。
llm 路径仅在无运行中事件循环的同步上下文（KB 检索端点线程池）可用；
异步上下文（智能体内部检索）自动回退 lexical——async 检索的模型重排待升级。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .tokenize import tokenize


def lexical_scores(query: str, contents: list[str]) -> list[float]:
    """词面覆盖度：查询词元（去重）在候选中的命中占比。"""
    q_tokens = {t for t in tokenize(query)}
    if not q_tokens:
        return [0.0] * len(contents)
    out = []
    for content in contents:
        c_tokens = set(tokenize(content))
        out.append(len(q_tokens & c_tokens) / len(q_tokens))
    return out


def lexical_rerank(query: str, candidates: list[tuple[int, str]]) -> list[int]:
    """按词面覆盖度降序返回候选索引（并列保持召回序）。"""
    scores = lexical_scores(query, [c for _, c in candidates])
    return sorted(range(len(candidates)), key=lambda i: -scores[i])


async def _llm_rerank_async(db: Session, query: str,
                            candidates: list[tuple[int, str]]) -> list[int]:
    from ..modelhub.router import hub

    listing = "\n".join(f"[{i}] {c[:200]}" for i, (_, c) in enumerate(candidates))
    system = ("EAP-RERANK 你是检索重排器。依据查询与候选的相关性打分并排序。"
              '只输出一个 JSON 数组，禁止其他文字：[{"idx": 0, "score": 10}, ...]'
              "（idx 为候选编号，score 为 0-10 相关性分）")
    user = f"【查询】\n{query}\n\n【候选】\n{listing}"
    completion = await hub.complete(db, [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
                                    capability="chat")
    raw = completion.result.content or ""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("裁判输出无 JSON 数组")
    scored = json.loads(raw[start:end + 1])
    order = sorted(scored, key=lambda x: -float(x.get("score", 0)))
    idxs = [int(x["idx"]) for x in order if 0 <= int(x["idx"]) < len(candidates)]
    if len(set(idxs)) != len(candidates):
        raise ValueError("重排结果不完整")
    return idxs


def llm_rerank(db: Session, query: str, candidates: list[tuple[int, str]]) -> list[int]:
    """LLM listwise 重排；模型调用/解析任何失败 → 回退 lexical（确定性降级）。"""
    import asyncio

    try:
        return asyncio.run(_llm_rerank_async(db, query, candidates))
    except Exception:
        return lexical_rerank(query, candidates)


def rerank_candidates(db: Session, query: str, candidates: list[tuple[int, str]],
                      method: str) -> list[int]:
    """重排分发：lexical | llm。

    llm 仅在无运行中事件循环的同步上下文（KB 检索端点线程池）真正调模型；
    异步上下文（智能体内部检索）直接回退 lexical——避免在运行中 loop 上嵌套 run。
    """
    if method == "llm":
        import asyncio

        try:
            asyncio.get_running_loop()
            return lexical_rerank(query, candidates)  # 异步上下文：确定性回退
        except RuntimeError:
            pass  # 无运行中 loop（同步线程池）→ asyncio.run 调模型
        return llm_rerank(db, query, candidates)
    return lexical_rerank(query, candidates)

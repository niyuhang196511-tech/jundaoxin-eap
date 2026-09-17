"""知识中心服务：摄入（分块+嵌入+图谱索引）、三路检索（BM25+向量+图谱 RRF）、级联删除。

分块/重排组件经 components 注册表解析（KB.pipeline 选型，插件可注入自定义实现）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Chunk, Document, KB
from .components import get_chunker, get_reranker
from .embedding import get_embedder
from .graph import delete_graph_for_doc, graph_recall, index_chunk_graph
from .retrieval import bm25_scores, rrf_combine, top_n
from .tokenize import tokenize
from .vector_store import get_vector_store


def _kb_pipeline(kb: KB) -> dict:
    return kb.pipeline if isinstance(kb.pipeline, dict) else {}


def ingest_text(db: Session, kb: KB, title: str, text: str, source: str = "", meta: dict | None = None) -> Document:
    """文档摄入：分块 → 嵌入（本地+向量库上行）→ 图谱索引 → 入库（docs/04 §1.2 三路索引）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    store = get_vector_store(settings)
    pipeline = _kb_pipeline(kb).get("chunker") or {}
    chunk = get_chunker(pipeline.get("name"), pipeline.get("params"))
    # M15：缓存原文到 meta（重摄入端点依赖；上限 2MB 防超大文档撑爆行存储）
    doc_meta = dict(meta or {})
    if len(text) <= 2 * 1024 * 1024:
        doc_meta.setdefault("original_text", text)
    doc = Document(kb_id=kb.id, title=title, source=source, meta=doc_meta)
    db.add(doc)
    db.flush()
    for i, piece in enumerate(chunk(text)):
        db.add(Chunk(
            kb_id=kb.id,
            doc_id=doc.id,
            idx=i,
            content=piece,
            embedding=embedder.embed(piece),
            meta={"type": "text"},
        ))
    db.flush()
    for c in db.scalars(select(Chunk).where(Chunk.doc_id == doc.id)).all():
        index_chunk_graph(db, kb.id, c.id, doc.id, c.content)
        store.upsert(kb.id, c.id, c.embedding or [])
    db.commit()
    return doc


def ingest_faq(db: Session, kb: KB, items: list[dict]) -> int:
    """FAQ 问答对整条成块，检索命中即答（客服模板，docs/04 §1.1）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    store = get_vector_store(settings)
    count = 0
    for item in items:
        q, a = item["question"], item["answer"]
        doc = Document(kb_id=kb.id, title=q, source="faq", meta={"type": "faq", "question": q})
        db.add(doc)
        db.flush()
        content = f"问：{q}\n答：{a}"
        chunk = Chunk(
            kb_id=kb.id, doc_id=doc.id, idx=0, content=content,
            embedding=embedder.embed(content),
            meta={"type": "faq", "question": q},
        )
        db.add(chunk)
        db.flush()
        index_chunk_graph(db, kb.id, chunk.id, doc.id, content)
        store.upsert(kb.id, chunk.id, chunk.embedding or [])
        count += 1
    db.commit()
    return count


def retrieve(db: Session, kb: KB, query: str, top_k: int = 5, rerank: str | None = None) -> list[dict]:
    """三路混合检索 + 可选两阶段重排（docs/04 §1.3）：

    BM25（稀疏）+ 向量（稠密，Milvus 或本地余弦）+ 图谱（多跳扩展）
    → RRF 融合（召回池 = top_k×3 至少 10 条）→ rerank="lexical"/"llm" 头部精排 → top_k。

    M15：OTel 启用时包 `kb.retrieve` span（挂在当前请求 span 下）。
    """
    from ..observability.tracing import enabled as otel_enabled, tracer

    span_cm = tracer().start_as_current_span(f"kb.retrieve {kb.name}") if otel_enabled() else None
    span = span_cm.__enter__() if span_cm is not None else None
    if span is not None:
        span.set_attribute("kb.name", kb.name)
        span.set_attribute("kb.query", query[:120])
    try:
        return _retrieve_inner(db, kb, query, top_k, rerank)
    except Exception as e:
        if span is not None:
            span.record_exception(e)
        raise
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)


def _retrieve_inner(db: Session, kb: KB, query: str, top_k: int, rerank: str | None) -> list[dict]:
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    store = get_vector_store(settings)
    chunks = db.scalars(select(Chunk).where(Chunk.kb_id == kb.id)).all()
    if not chunks:
        return []

    docs_tokens = [tokenize(c.content) for c in chunks]
    bm25 = bm25_scores(tokenize(query), docs_tokens)
    q_vec = embedder.embed(query)
    vec_scores = store.scores_for(chunks, q_vec)

    # 图谱路：chunk_id → score 映射到 chunk 索引（多跳扩展召回弱文本匹配的关联块）
    graph_chunk = graph_recall(db, kb.id, query)
    chunk_index = {c.id: i for i, c in enumerate(chunks)}
    graph_scores = [0.0] * len(chunks)
    for cid, s in graph_chunk.items():
        i = chunk_index.get(cid)
        if i is not None:
            graph_scores[i] = s

    fused = rrf_combine([top_n(bm25, len(chunks)), top_n(vec_scores, len(chunks)),
                         top_n(graph_scores, len(chunks))])
    pool_n = max(top_k * 3, 10)
    fused_top = sorted(fused.items(), key=lambda kv: -kv[1])[:pool_n]

    if rerank and len(fused_top) > 1:
        candidates = [(idx, chunks[idx].content) for idx, _ in fused_top]
        rr = _kb_pipeline(kb).get("reranker") or {}
        if rr.get("name"):  # KB 级自定义重排器（扩展注册表）
            order = get_reranker(rr.get("name"), rr.get("params"))(query, candidates)[:top_k]
        else:
            from .rerank import rerank_candidates

            order = rerank_candidates(db, query, candidates, rerank)
        ordered = [(candidates[i][0], fused_top[i][1]) for i in order][:top_k]
    else:
        ordered = fused_top[:top_k]

    doc_titles = {
        d.id: d.title
        for d in db.scalars(select(Document).where(Document.kb_id == kb.id)).all()
    }
    hits = []
    for idx, score in ordered:
        c = chunks[idx]
        hits.append({
            "content": c.content,
            "score": round(score, 6),
            "citation": {
                "kb": kb.name,
                "document": doc_titles.get(c.doc_id, ""),
                "chunk_id": c.id,
                "chunk_index": c.idx,
            },
        })
    return hits


def delete_document(db: Session, kb: KB, doc_id: int) -> int:
    """级联删除（docs/05 §4）：原文 → Chunk（向量随行）→ 图谱边与孤立节点 → 向量库。"""
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb.id:
        return 0
    delete_graph_for_doc(db, kb.id, doc_id)
    chunk_ids = [c.id for c in db.scalars(select(Chunk).where(Chunk.doc_id == doc_id)).all()]
    get_vector_store(get_settings()).delete(chunk_ids)
    n = 0
    for c in db.scalars(select(Chunk).where(Chunk.doc_id == doc_id)).all():
        db.delete(c)
        n += 1
    db.delete(doc)
    db.commit()
    return n

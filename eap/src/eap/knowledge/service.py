"""知识中心服务：摄入（分块+嵌入+图谱索引）、三路检索（BM25+向量+图谱 RRF）、级联删除。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Chunk, Document, KB
from .chunking import split_text
from .embedding import cosine, get_embedder
from .graph import delete_graph_for_doc, graph_recall, index_chunk_graph
from .retrieval import bm25_scores, rrf_combine, top_n
from .tokenize import tokenize
from .vector_store import get_vector_store


def ingest_text(db: Session, kb: KB, title: str, text: str, source: str = "", meta: dict | None = None) -> Document:
    """文档摄入：分块 → 嵌入（本地+向量库上行）→ 图谱索引 → 入库（docs/04 §1.2 三路索引）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    store = get_vector_store(settings)
    doc = Document(kb_id=kb.id, title=title, source=source, meta=meta or {})
    db.add(doc)
    db.flush()
    for i, piece in enumerate(split_text(text)):
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


def retrieve(db: Session, kb: KB, query: str, top_k: int = 5) -> list[dict]:
    """三路混合检索：BM25（稀疏）+ 向量（稠密，Milvus 或本地余弦）+ 图谱（多跳扩展）
    → RRF 融合 → Citation（docs/04 §1.3）。"""
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
    ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]

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

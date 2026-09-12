"""知识中心服务：摄入（分块+嵌入）、检索（混合融合+Citation）、级联删除。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Chunk, Document, KB
from .chunking import split_text
from .embedding import cosine, get_embedder
from .retrieval import bm25_scores, rrf_combine, top_n
from .tokenize import tokenize


def ingest_text(db: Session, kb: KB, title: str, text: str, source: str = "", meta: dict | None = None) -> Document:
    """文档摄入：分块 → 嵌入 → 入库（Ingestion 管道的 M1 版，docs/04 §1.2）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
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
    return doc


def ingest_faq(db: Session, kb: KB, items: list[dict]) -> int:
    """FAQ 问答对整条成块，检索命中即答（客服模板，docs/04 §1.1）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    count = 0
    for item in items:
        q, a = item["question"], item["answer"]
        doc = Document(kb_id=kb.id, title=q, source="faq", meta={"type": "faq", "question": q})
        db.add(doc)
        db.flush()
        content = f"问：{q}\n答：{a}"
        db.add(Chunk(
            kb_id=kb.id, doc_id=doc.id, idx=0, content=content,
            embedding=embedder.embed(content),
            meta={"type": "faq", "question": q},
        ))
        count += 1
    db.flush()
    return count


def retrieve(db: Session, kb: KB, query: str, top_k: int = 5) -> list[dict]:
    """混合检索：BM25（稀疏）+ 向量（稠密）→ RRF 融合 → 带 Citation 的命中（docs/04 §1.3）。"""
    settings = get_settings()
    embedder = get_embedder(kb.embedding_provider or settings.embedding_provider, settings)
    chunks = db.scalars(select(Chunk).where(Chunk.kb_id == kb.id)).all()
    if not chunks:
        return []

    docs_tokens = [tokenize(c.content) for c in chunks]
    bm25 = bm25_scores(tokenize(query), docs_tokens)
    q_vec = embedder.embed(query)
    vec_scores = [cosine(q_vec, c.embedding or []) for c in chunks]

    fused = rrf_combine([top_n(bm25, len(chunks)), top_n(vec_scores, len(chunks))])
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
    """级联删除：原文 → Chunk（向量随行删除）；图谱/缓存级联在 M2（docs/05 §4）。"""
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb.id:
        return 0
    n = 0
    for c in db.scalars(select(Chunk).where(Chunk.doc_id == doc_id)).all():
        db.delete(c)
        n += 1
    db.delete(doc)
    db.commit()
    return n

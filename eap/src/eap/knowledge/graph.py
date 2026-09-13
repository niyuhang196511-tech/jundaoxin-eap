"""知识图谱路（docs/04 §1 三路索引之图谱路，M3 离线 MVP；LightRAG 式设计）。

离线 MVP：实体 = 词元级（Han 双字组 + ASCII 词，去停用词，单字噪声大不建）；
关系 = chunk 内共现（成对加权边）。多跳检索：查询词元锚定实体 → 沿边 k 跳扩展 →
按边权聚合找回关联 chunk → 作为第三路并入 RRF。
真实实体/关系抽取（LLM 结构化输出）配置真实模型后可无缝替换 index_chunk_graph。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GraphEdgeRecord, GraphNodeRecord
from .tokenize import tokenize

# 单字噪声大：只收长度 >=2 的词元（Han 双字组与 ASCII 词已由 tokenize 保证）
_MIN_ENTITY_LEN = 2
_MAX_ENTITIES_PER_CHUNK = 40
_STOP = frozenset({"的是", "了在", "一个", "这个", "可以", "没有", "我们", "你们", "它们",
                   "以及", "或者", "并且", "但是", "如果", "因为", "所以", "对于", "关于"})


def extract_entities(content: str) -> list[str]:
    """词元 → 去重实体（保持出现顺序，限流防长文组合爆炸）。"""
    seen: dict[str, None] = {}
    for t in tokenize(content):
        if len(t) >= _MIN_ENTITY_LEN and t not in _STOP:
            seen[t] = None
            if len(seen) >= _MAX_ENTITIES_PER_CHUNK:
                break
    return list(seen)


def index_chunk_graph(db: Session, kb_id: int, chunk_id: int, doc_id: int, content: str) -> int:
    """chunk 实体/共现边入图（幂等：先清该 chunk 旧边）。返回新建边数。"""
    for e in db.scalars(select(GraphEdgeRecord).where(GraphEdgeRecord.chunk_id == chunk_id)).all():
        db.delete(e)
    entities = extract_entities(content)
    if len(entities) < 2:
        return 0
    for ent in entities:
        node = db.scalar(select(GraphNodeRecord).where(
            GraphNodeRecord.kb_id == kb_id, GraphNodeRecord.entity == ent))
        if node is None:
            db.add(GraphNodeRecord(kb_id=kb_id, entity=ent))
    created = 0
    for i, src in enumerate(entities):
        for dst in entities[i + 1:]:
            db.add(GraphEdgeRecord(kb_id=kb_id, src=src, dst=dst, weight=1,
                                   chunk_id=chunk_id, doc_id=doc_id))
            created += 1
    db.flush()
    return created


def delete_graph_for_doc(db: Session, kb_id: int, doc_id: int) -> int:
    """级联删除（docs/05 §4）：删文档所有边 + 孤立节点。返回删除边数。"""
    edges = db.scalars(select(GraphEdgeRecord).where(
        GraphEdgeRecord.kb_id == kb_id, GraphEdgeRecord.doc_id == doc_id)).all()
    n = 0
    for e in edges:
        db.delete(e)
        n += 1
    db.flush()
    # 孤立节点清理：kb 内已无任何边触达的实体
    live = {r for row in db.scalars(select(GraphEdgeRecord).where(
        GraphEdgeRecord.kb_id == kb_id)).all() for r in (row.src, row.dst)}
    for node in db.scalars(select(GraphNodeRecord).where(GraphNodeRecord.kb_id == kb_id)).all():
        if node.entity not in live:
            db.delete(node)
    db.flush()
    return n


def graph_recall(db: Session, kb_id: int, query: str, hops: int = 1, limit: int = 60) -> dict[int, float]:
    """图谱召回：查询词元锚定实体 → 沿边 hops 跳扩展 → chunk 按＂边权 × 2^(-跳数)＂聚合。

    返回 {chunk_id: score}。锚定实体自身所在 chunk 也计入（hop 0）。
    """
    q_tokens = {t for t in tokenize(query) if len(t) >= _MIN_ENTITY_LEN}
    if not q_tokens:
        return {}
    nodes = db.scalars(select(GraphNodeRecord).where(GraphNodeRecord.kb_id == kb_id)).all()
    anchors = [n.entity for n in nodes if n.entity in q_tokens]
    if not anchors:
        return {}

    # BFS 沿边扩展
    frontier = set(anchors)
    visited: set[str] = set(anchors)
    edge_score: dict[int, float] = {}  # edge_id → score
    current_hop = 0
    while frontier and current_hop <= hops:
        weight_scale = 1.0 / (2 ** current_hop)
        related = db.scalars(select(GraphEdgeRecord).where(
            GraphEdgeRecord.kb_id == kb_id,
            GraphEdgeRecord.src.in_(frontier) | GraphEdgeRecord.dst.in_(frontier))).all()
        next_frontier: set[str] = set()
        for e in related:
            edge_score[e.id] = edge_score.get(e.id, 0.0) + weight_scale * e.weight
            other = e.dst if e.src in frontier else e.src
            if other not in visited:
                next_frontier.add(other)
        visited |= next_frontier
        frontier = next_frontier
        current_hop += 1

    # 边分聚合到 chunk
    chunk_score: dict[int, float] = {}
    for e in db.scalars(select(GraphEdgeRecord).where(
            GraphEdgeRecord.kb_id == kb_id, GraphEdgeRecord.id.in_(list(edge_score)))).all():
        chunk_score[e.chunk_id] = chunk_score.get(e.chunk_id, 0.0) + edge_score[e.id]
    ranked = dict(sorted(chunk_score.items(), key=lambda kv: -kv[1])[:limit])
    return ranked


def graph_overview(db: Session, kb_id: int, limit: int = 50) -> dict:
    """图谱概览（画布/观测用）：节点清单 + 权重最高的边。"""
    nodes = [n.entity for n in db.scalars(select(GraphNodeRecord).where(
        GraphNodeRecord.kb_id == kb_id)).all()][:limit]
    edges = db.scalars(select(GraphEdgeRecord).where(
        GraphEdgeRecord.kb_id == kb_id).order_by(GraphEdgeRecord.weight.desc())).all()[:limit]
    return {"nodes": nodes, "edges": [
        {"src": e.src, "dst": e.dst, "weight": e.weight, "chunk_id": e.chunk_id} for e in edges]}

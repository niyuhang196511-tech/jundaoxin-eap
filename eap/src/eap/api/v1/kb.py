"""知识中心 API：KB 生命周期 + 摄入 + 混合检索。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...knowledge import service as kb_svc
from ...models import KB
from ...schemas import (
    DocIngest, FAQIngest, KBCreate, RetrieveRequest, RetrieveHit, RetrieveResponse,
)
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/kb",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


def _get_kb(db: Session, name: str) -> KB:
    kb = db.scalar(select(KB).where(KB.name == name))
    if kb is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 知识库 {name} 不存在")
    return kb


@router.get("")
def list_kbs(request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    # 租户过滤（M6）：NULL=平台共享对所有人可见；JWT 登录租户额外可见自己的库。
    # API Key/dev 通道保持全量可见，兼容单租户部署。
    from sqlalchemy import or_

    tenant_id = getattr(request.state, "tenant_id", None)
    is_jwt = getattr(request.state, "auth_kind", "") == "jwt"
    q = select(KB)
    if is_jwt and tenant_id:
        q = q.where(or_(KB.tenant_id.is_(None), KB.tenant_id == tenant_id))
    return [
        {"id": kb.id, "name": kb.name, "title": kb.title, "template": kb.template,
         "embedding_provider": kb.embedding_provider}
        for kb in db.scalars(q).all()
    ]


@router.post("")
def create_kb(body: KBCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(KB).where(KB.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 知识库 {body.name} 已存在")
    # JWT 通道创建的知识库归属该租户；API Key 通道 = 平台共享（NULL）
    tenant_id = getattr(request.state, "tenant_id", None) \
        if getattr(request.state, "auth_kind", "") == "jwt" else None
    kb = KB(name=body.name, title=body.title, template=body.template,
            pipeline=body.pipeline, tenant_id=tenant_id)
    db.add(kb)
    db.commit()
    return {"name": kb.name, "title": kb.title, "template": kb.template}


@router.get("/{name}/documents")
def list_documents(name: str, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    from ...models import Document

    docs = db.scalars(select(Document).where(Document.kb_id == kb.id)).all()
    return [{"id": d.id, "title": d.title, "source": d.source, "meta": d.meta} for d in docs]


@router.get("/{name}/documents/{doc_id}/chunks")
def list_chunks(name: str, doc_id: int, db: Session = fastapi.Depends(get_db)):
    """分块预览（M14）：文档的 chunk 列表（内容 + 索引 + 元信息），知识库详情抽屉数据源。"""
    kb = _get_kb(db, name)
    from ...models import Chunk

    chunks = db.scalars(
        select(Chunk).where(Chunk.kb_id == kb.id, Chunk.doc_id == doc_id)
        .order_by(Chunk.idx.asc())
    ).all()
    return [{"id": c.id, "idx": c.idx, "content": c.content, "meta": c.meta} for c in chunks]


@router.post("/{name}/documents")
def ingest_document(name: str, body: DocIngest, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    doc = kb_svc.ingest_text(db, kb, body.title, body.text, source=body.source)
    db.commit()
    return {"document_id": doc.id, "title": doc.title, "kb": kb.name}


@router.post("/{name}/upload")
async def upload_document(
    name: str,
    request: fastapi.Request,
    file: fastapi.UploadFile = fastapi.File(...),
    title: str = fastapi.Form(""),
    parser: str = fastapi.Form(""),
    db: Session = fastapi.Depends(get_db),
):
    """文件上传摄入（M12/M14）：pdf/docx/txt/md → 按指定后端解析 → 异步摄入。

    parser: local（默认）| mineru_cloud | mineru_selfhosted——扫描件/复杂版式用 MinerU。
    返回 task_id；摄入状态经 GET /api/v1/tasks/{task_id} 轮询。
    """
    import base64

    kb = _get_kb(db, name)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise fastapi.HTTPException(status_code=413, detail="文件超过 20MB 上限")
    filename = file.filename or "doc.txt"
    doc_title = title or filename.rsplit(".", 1)[0]

    from .tasks import _engine

    task_id = await _engine(request).submit(db, "kb.ingest", {
        "kb": kb.name, "title": doc_title, "filename": filename,
        "file_b64": base64.b64encode(data).decode(), "source": "upload",
        "parser": parser,
    })
    return {"task_id": task_id, "kb": kb.name, "filename": filename,
            "note": "解析与摄入异步执行，经任务端点轮询状态"}


@router.post("/{name}/faq")
def ingest_faq(name: str, body: FAQIngest, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    n = kb_svc.ingest_faq(db, kb, [i.model_dump() for i in body.items])
    db.commit()
    return {"kb": kb.name, "ingested": n}


@router.post("/{name}/retrieve")
def retrieve(name: str, body: RetrieveRequest, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    hits = kb_svc.retrieve(db, kb, body.query, top_k=body.top_k, rerank=body.rerank)
    return RetrieveResponse(
        kb=kb.name,
        hits=[RetrieveHit(
            content=h["content"], score=h["score"],
            citation={**h["citation"], "chunk_index": h["citation"].get("chunk_index", 0)},
        ) for h in hits],
    )


@router.get("/{name}/graph")
def graph_overview(name: str, db: Session = fastapi.Depends(get_db)):
    """图谱概览：实体节点 + 高权重共现边（三路索引之图谱路观测）。"""
    kb = _get_kb(db, name)
    from ...knowledge.graph import graph_overview as overview

    return {"kb": kb.name, **overview(db, kb.id)}


@router.delete("/{name}/documents/{doc_id}")
def delete_document(name: str, doc_id: int, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    n = kb_svc.delete_document(db, kb, doc_id)
    if n == 0:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 文档 {doc_id} 不存在")
    return {"deleted_chunks": n}


@router.post("/{name}/documents/{doc_id}/reingest")
def reingest_document(name: str, doc_id: int, request: fastapi.Request,
                      db: Session = fastapi.Depends(get_db)):
    """文档重摄入（M15）：按缓存原文重新解析+分块+嵌入（删除旧块后新建）。

    适用：解析后端切换（如换 MinerU）/分块参数调整后刷新既有文档。
    前提：摄入时缓存了原文（M15 起的摄入自动缓存 original_text/original_file_b64 到 meta）。
    """
    from ...models import Document
    from .tasks import _engine

    kb = _get_kb(db, name)
    doc = db.scalar(select(Document).where(Document.kb_id == kb.id, Document.id == doc_id))
    if doc is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 文档 {doc_id} 不存在")
    meta = doc.meta or {}
    original_text = str(meta.get("original_text", ""))
    if not original_text:
        raise fastapi.HTTPException(
            status_code=409,
            detail="EAP-4009 该文档无原文缓存（M15 前摄入），请删除后重新上传/粘贴")

    payload = {"kb": kb.name, "title": doc.title, "text": original_text,
               "parser": str(meta.get("parser") or ""), "source": doc.source}
    if meta.get("original_file_b64"):
        payload["file_b64"] = meta["original_file_b64"]
        payload["filename"] = str(meta.get("original_filename", "doc"))

    # 先删旧文档（级联 chunk/图谱/向量），任务执行时以当前解析后端/分块配置重建
    kb_svc.delete_document(db, kb, doc_id)
    db.commit()
    task_id = _engine(request).submit(db, "kb.ingest", payload)
    return {"task_id": task_id, "kb": kb.name, "document_id": doc_id,
            "note": "重摄入异步执行，经任务端点轮询"}

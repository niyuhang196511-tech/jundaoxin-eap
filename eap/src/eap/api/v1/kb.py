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
def create_kb(body: KBCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(KB).where(KB.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 知识库 {body.name} 已存在")
    kb = KB(name=body.name, title=body.title, template=body.template, pipeline=body.pipeline)
    db.add(kb)
    db.commit()
    return {"name": kb.name, "title": kb.title, "template": kb.template}


@router.get("/{name}/documents")
def list_documents(name: str, db: Session = fastapi.Depends(get_db)):
    kb = _get_kb(db, name)
    from ...models import Document

    docs = db.scalars(select(Document).where(Document.kb_id == kb.id)).all()
    return [{"id": d.id, "title": d.title, "source": d.source, "meta": d.meta} for d in docs]


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
    db: Session = fastapi.Depends(get_db),
):
    """文件上传摄入（M12）：pdf/docx/txt/md → 解析 → 异步摄入（任务引擎，不阻塞请求）。

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

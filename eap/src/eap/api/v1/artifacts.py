"""Artifacts API（docs/unfinished v0.5-⑦）：产物列表/预览/下载（凭 id 寻址）。"""

from __future__ import annotations

import fastapi
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ...db import get_db
from ...runtime.artifacts import artifact_file, get_artifact, list_artifacts
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/artifacts",
                           dependencies=[fastapi.Depends(resolve_tenant),
                                         fastapi.Depends(require_api_key)])


@router.get("")
def list_artifacts_view(session_id: str | None = None, agent: str | None = None,
                        db: Session = fastapi.Depends(get_db)):
    """产物列表（过期惰性剔除）。"""
    return [
        {"id": r.id, "name": r.name, "type": r.type, "mime": r.mime, "size": r.size,
         "agent": r.agent, "session_id": r.session_id, "task_id": r.task_id,
         "created_at": str(r.created_at), "expires_at": str(r.expires_at) if r.expires_at else None}
        for r in list_artifacts(db, session_id=session_id, agent=agent)
    ]


@router.get("/{artifact_id}/preview")
def preview_artifact(artifact_id: str, db: Session = fastapi.Depends(get_db)):
    """产物预览：文本类内联返回，图片经静态媒体路径，二进制给下载提示。"""
    record = get_artifact(db, artifact_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 产物 {artifact_id} 不存在或已过期")
    path = artifact_file(db, record)
    if path is None:
        raise fastapi.HTTPException(status_code=410, detail=f"EAP-4010 产物 {artifact_id} 文件已缺失")
    if record.mime.startswith("text/") or record.mime == "application/json":
        from pathlib import Path

        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return {"id": record.id, "name": record.name, "type": record.type,
                "mime": record.mime, "content": text[:200000]}
    # 图片：走 FileResponse 内联
    return FileResponse(path, media_type=record.mime, filename=record.name)


@router.get("/{artifact_id}/download")
def download_artifact(artifact_id: str, db: Session = fastapi.Depends(get_db)):
    """产物下载（Attachment 头，按服务端 id 寻址，不接受客户端路径）。"""
    record = get_artifact(db, artifact_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 产物 {artifact_id} 不存在或已过期")
    path = artifact_file(db, record)
    if path is None:
        raise fastapi.HTTPException(status_code=410, detail=f"EAP-4010 产物 {artifact_id} 文件已缺失")
    return FileResponse(path, media_type=record.mime, filename=record.name)

"""Artifact / File Center（docs/unfinished v0.5-⑦）：Agent 产物的统一存储/预览/下载。

- 存储：EAP_MEDIA_DIR/artifacts/<id>/<filename>（复用 media 目录约定，按 id 寻址）
- 记录：artifacts 表（type/mime/size/sha256/ttl），过期惰性不返回
- SDK：ctx.artifacts.create(name, type, content, mime) → InvokeResult.artifacts 引用
- 穿越防线：id 与文件名均白名单校验，读写路径规范化后必须位于 artifacts 根目录之内
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ArtifactRecord

# 类型 → (扩展名默认, mime 默认)
TYPE_DEFAULTS = {
    "pdf": (".pdf", "application/pdf"),
    "docx": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": (".csv", "text/csv"),
    "json": (".json", "application/json"),
    "md": (".md", "text/markdown"),
    "image": (".png", "image/png"),
    "chart": (".svg", "image/svg+xml"),
    "report": (".md", "text/markdown"),
}

_ART_ID_RE = re.compile(r"art-[0-9a-f]{12}")
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


def _artifacts_root() -> Path:
    root = Path(get_settings().media_dir) / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_artifact_path(art_id: str, filename: str) -> Path:
    """artifacts 目录内的规范化路径：id/文件名白名单校验 + 规范化越界防线。"""
    if not _ART_ID_RE.fullmatch(art_id):
        raise ValueError(f"产物 id 非法: {art_id!r}")
    if not _FILENAME_RE.fullmatch(filename):
        raise ValueError(f"产物文件名非法: {filename!r}")
    root = _artifacts_root().resolve()
    abs_path = (root / art_id / filename).resolve()
    if not str(abs_path).startswith(str(root) + os.sep):
        raise ValueError("产物路径越界，已拒绝")
    return abs_path


def create_artifact(db: Session, *, name: str, type: str, content: bytes | str,
                    mime: str | None = None, agent: str | None = None,
                    session_id: str | None = None, task_id: str | None = None,
                    ttl_hours: int | None = None, trace_id: str = "") -> ArtifactRecord:
    """创建产物：内容落盘 + 记录入库（返回记录，前端凭 id 预览/下载）。"""
    if type not in TYPE_DEFAULTS:
        raise ValueError(f"不支持的产物类型 {type}（允许: {sorted(TYPE_DEFAULTS)}）")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not content:
        raise ValueError("产物内容为空")
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("产物超过 20MB 上限")

    default_ext, default_mime = TYPE_DEFAULTS[type]
    art_id = "art-" + uuid.uuid4().hex[:12]
    filename = f"{name}{default_ext}" if not name.endswith(default_ext) else name
    if not _FILENAME_RE.fullmatch(filename):
        filename = f"artifact{default_ext}"

    path = _safe_artifact_path(art_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    record = ArtifactRecord(
        id=art_id, name=name, type=type,
        mime=mime or default_mime, size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        agent=agent or "", session_id=session_id, task_id=task_id,
        storage_path=f"{art_id}/{filename}",
        expires_at=(datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=ttl_hours)) if ttl_hours else None,
        trace_id=trace_id,
    )
    db.add(record)
    db.flush()
    return record


def get_artifact(db: Session, artifact_id: str) -> ArtifactRecord | None:
    """取未过期的产物记录（过期惰性剔除）。"""
    record = db.get(ArtifactRecord, artifact_id)
    if record is None:
        return None
    if record.expires_at is not None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if now >= record.expires_at:
            return None
    return record


def artifact_file(db: Session, record: ArtifactRecord) -> str | None:
    """记录 → 磁盘绝对路径（basename 重建 + 白名单校验，穿越防线同写入路径）。"""
    filename = os.path.basename(record.storage_path.replace("\\", "/"))
    try:
        abs_path = _safe_artifact_path(record.id, filename)
    except (ValueError, OSError):
        return None
    return str(abs_path) if abs_path.exists() else None


def list_artifacts(db: Session, *, session_id: str | None = None,
                   agent: str | None = None, limit: int = 50) -> list[ArtifactRecord]:
    query = select(ArtifactRecord).order_by(ArtifactRecord.created_at.desc()).limit(limit)
    if session_id:
        query = query.where(ArtifactRecord.session_id == session_id)
    if agent:
        query = query.where(ArtifactRecord.agent == agent)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return [r for r in db.scalars(query).all()
            if r.expires_at is None or now < r.expires_at]

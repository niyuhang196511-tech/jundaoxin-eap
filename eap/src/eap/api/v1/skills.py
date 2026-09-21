"""技能中心 API：注册 / 目录（L1）/ 详情（L2）/ 停用 / 技能包打包与签名导入（docs/04 §3）。

附件（M34，L5 工程部分）：导入带 scripts/assets 附件的技能包 → 清单入库 + 文件落盘；
GET /{name}/assets 列清单、GET /{name}/assets/download?path= 按清单取回
（路径安全面 EAP-8104：拒绝绝对路径/`..`/非白名单，清单外一律 404）。
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...observability import audit
from ...models import SkillRecord
from ...runtime import skill_files, skill_pkg
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/skills",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class SkillCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = "1.0.0"
    description: str = ""
    instructions: str = Field(min_length=1, description="技能指令全文（L2 渐进披露正文）")
    permissions: list[str] = Field(default_factory=list)


@router.get("")
def list_skills(db: Session = fastapi.Depends(get_db)):
    """L1 目录：只暴露 name + description（渐进披露第一级，控制上下文成本）。"""
    return [
        {"name": s.name, "version": s.version, "description": s.description, "enabled": s.enabled}
        for s in db.scalars(select(SkillRecord)).all()
    ]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def create_skill(body: SkillCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(SkillRecord).where(SkillRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 技能 {body.name} 已存在")
    skill = SkillRecord(
        name=body.name, version=body.version, description=body.description,
        instructions=body.instructions, permissions=body.permissions,
    )
    db.add(skill)
    db.commit()
    audit.record("skill.create", actor=audit.actor_of(request), target=skill.name,
                 detail={"version": skill.version}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": skill.name, "version": skill.version, "status": "registered"}


# ---------- 技能包（技能市场地基）：打包签名 / 验签导入 / 公钥分发 ----------
# 注意：静态路径必须注册在 /{name} 之前，否则被路径参数匹配吞掉

@router.get("/public-key")
def signing_public_key():
    """签名公钥（hex）：Harness / 第三方据此本地校验技能包。"""
    return {"public_key": skill_pkg.public_key_hex(get_settings().skill_signing_key)}


class SkillImport(BaseModel):
    bundle: dict = Field(description="技能包 bundle（format/skill/signature）")


@router.post("/import", dependencies=[fastapi.Depends(require_admin)])
def import_skill(body: SkillImport, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """导入技能包：验签失败 401（EAP-8101）；附件安全面/清单不符 400（EAP-8104）；
    导入后默认停用，人工审查后启用。附件整体替换：落盘 + 清单入库 + 审计。"""
    try:
        skill = skill_pkg.verify_bundle(body.bundle, get_settings().skill_signing_key)
    except PermissionError as e:
        raise fastapi.HTTPException(status_code=401, detail=str(e)) from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    record = db.scalar(select(SkillRecord).where(SkillRecord.name == skill["name"]))
    if record is None:
        record = SkillRecord(name=skill["name"])
        db.add(record)
    record.version = skill["version"]
    record.description = skill.get("description", "")
    record.instructions = skill["instructions"]
    record.permissions = skill.get("permissions", [])
    record.assets = skill.get("assets") or []
    record.enabled = False  # 导入不可信来源内容：审查后人工启用
    files = skill_pkg.decode_bundle_files(body.bundle)
    if files:
        skill_files.save_assets(record.name, files)  # 落盘前再过一次同一安全面（纵深防御）
    else:
        # 整体替换语义：新包无附件 → 清场（此时 record.assets 已被覆盖为空，
        # 不能以它为条件判断——清场依据是「旧包可能有附件」，remove_assets 对无目录安全）
        skill_files.remove_assets(record.name)
    if record.assets:
        audit.record("skill.assets.import", actor=audit.actor_of(request), target=record.name,
                     detail={"files": len(record.assets),
                             "total_bytes": sum(a.get("size", 0) for a in record.assets)},
                     trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    return {"name": record.name, "version": record.version, "status": "imported-disabled",
            "assets": len(record.assets)}


@router.get("/{name}")
def get_skill(name: str, db: Session = fastapi.Depends(get_db)):
    """L2 详情：完整指令文本（Agent 显式引用时才加载）；M34 附带附件清单。"""
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    return {
        "name": skill.name, "version": skill.version, "description": skill.description,
        "instructions": skill.instructions, "permissions": skill.permissions,
        "assets": skill.assets or [], "enabled": skill.enabled,
    }


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_skill(name: str, enabled: bool, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    skill.enabled = enabled
    db.commit()
    audit.record("skill.toggle", actor=audit.actor_of(request), target=name,
                 detail={"enabled": enabled}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "enabled": enabled}


# ---------- 附件（M34）：清单列表 / 按白名单取回 ----------

def _get_skill_or_404(db: Session, name: str) -> SkillRecord:
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    return skill


@router.get("/{name}/assets")
def list_skill_assets(name: str, db: Session = fastapi.Depends(get_db)):
    """附件清单（path/size/sha256）：清单在导入时已验签，这里只读库内事实。"""
    skill = _get_skill_or_404(db, name)
    return {"name": skill.name, "version": skill.version,
            "assets": [{"path": a.get("path"), "size": a.get("size"), "sha256": a.get("sha256")}
                       for a in (skill.assets or [])]}


@router.get("/{name}/assets/download")
def download_skill_asset(name: str, path: str, db: Session = fastapi.Depends(get_db)):
    """按白名单取回附件：路径不合法 400（EAP-8104），清单外/盘上缺失 404。

    清单（导入时已验签）是唯一事实源——盘上多出的文件不参与下载。
    """
    skill = _get_skill_or_404(db, name)
    try:
        normalized = skill_pkg.validate_asset_path(path)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    item = next((a for a in (skill.assets or []) if a.get("path") == normalized), None)
    if item is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 附件不在清单内: {normalized}")
    content = skill_files.load_asset(skill.name, normalized)
    if content is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 附件文件缺失: {normalized}")
    media_type = mimetypes.guess_type(normalized)[0] or "application/octet-stream"
    return fastapi.Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(normalized.rsplit('/', 1)[-1])}"})


# ---------- 技能包导出 ----------

@router.get("/{name}/package")
def package_skill(name: str, db: Session = fastapi.Depends(get_db)):
    """导出签名技能包：SKILL.md（人读）+ signature（机器验）+ scripts/assets 附件（M34）。

    附件清单随 skill["assets"] 入签；打包前先复核盘上文件与库内清单一致
    （防磁盘篡改/缺失，不一致 409 请重导入）。
    """
    skill = _get_skill_or_404(db, name)
    files = None
    if skill.assets:
        files = skill_files.load_all(skill.name)
        # 清单是无序集合：load_all 按磁盘路径排序返回，与导入时原始顺序逐项比对会误报 409
        stored = sorted(skill.assets, key=lambda a: a.get("path", ""))
        built = sorted(skill_pkg.build_asset_manifest(files), key=lambda a: a.get("path", "")) if files else []
        if not files or built != stored:
            raise fastapi.HTTPException(
                status_code=409,
                detail=f"EAP-4009 技能 {name} 附件文件缺失或与清单不符，请重新导入技能包")
    bundle = skill_pkg.sign_bundle(
        {"name": skill.name, "version": skill.version, "description": skill.description,
         "instructions": skill.instructions, "permissions": skill.permissions},
        get_settings().skill_signing_key, files=files)
    return bundle

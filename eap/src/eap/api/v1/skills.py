"""技能中心 API：注册 / 目录（L1）/ 详情（L2）/ 停用 / 技能包打包与签名导入（docs/04 §3）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...models import SkillRecord
from ...runtime import skill_pkg
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
def create_skill(body: SkillCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(SkillRecord).where(SkillRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 技能 {body.name} 已存在")
    skill = SkillRecord(
        name=body.name, version=body.version, description=body.description,
        instructions=body.instructions, permissions=body.permissions,
    )
    db.add(skill)
    db.commit()
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
def import_skill(body: SkillImport, db: Session = fastapi.Depends(get_db)):
    """导入技能包：验签失败 401（EAP-8101）；导入后默认停用，人工审查后启用。"""
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
    record.enabled = False  # 导入不可信来源内容：审查后人工启用
    db.commit()
    return {"name": record.name, "version": record.version, "status": "imported-disabled"}


@router.get("/{name}")
def get_skill(name: str, db: Session = fastapi.Depends(get_db)):
    """L2 详情：完整指令文本（Agent 显式引用时才加载）。"""
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    return {
        "name": skill.name, "version": skill.version, "description": skill.description,
        "instructions": skill.instructions, "permissions": skill.permissions,
        "enabled": skill.enabled,
    }


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_skill(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    skill.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}


# ---------- 技能包导出 ----------

@router.get("/{name}/package")
def package_skill(name: str, db: Session = fastapi.Depends(get_db)):
    """导出签名技能包：SKILL.md（人读）+ signature（机器验），可直接分发到市场。"""
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    bundle = skill_pkg.sign_bundle(
        {"name": skill.name, "version": skill.version, "description": skill.description,
         "instructions": skill.instructions, "permissions": skill.permissions},
        get_settings().skill_signing_key)
    return bundle

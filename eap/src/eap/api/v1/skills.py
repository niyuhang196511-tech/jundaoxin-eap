"""技能中心 API：注册 / 目录（L1 渐进披露）/ 详情（L2）/ 停用（docs/04 §3）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import SkillRecord
from ..deps import require_api_key, resolve_tenant

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


@router.post("")
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


@router.patch("/{name}")
def toggle_skill(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    skill = db.scalar(select(SkillRecord).where(SkillRecord.name == name))
    if skill is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 技能 {name} 不存在")
    skill.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

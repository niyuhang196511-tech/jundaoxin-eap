"""Prompt Center API：模板注册（变量自动提取）/ 渲染 / 停用（docs/05 §3）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import PromptRecord
from ...runtime.context import extract_prompt_variables, render_prompt
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/prompts",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class PromptCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = "1.0.0"
    description: str = ""
    template: str = Field(min_length=1)


class PromptRenderRequest(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict)


@router.get("")
def list_prompts(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": p.name, "version": p.version, "description": p.description,
         "variables": p.variables, "enabled": p.enabled}
        for p in db.scalars(select(PromptRecord)).all()
    ]


@router.post("")
def create_prompt(body: PromptCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(PromptRecord).where(PromptRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 Prompt {body.name} 已存在")
    record = PromptRecord(
        name=body.name, version=body.version, description=body.description,
        template=body.template, variables=extract_prompt_variables(body.template),
    )
    db.add(record)
    db.commit()
    return {"name": record.name, "variables": record.variables, "status": "registered"}


@router.post("/{name}/render")
def render(name: str, body: PromptRenderRequest, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    try:
        text = render_prompt(record.template, body.variables)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return {"name": name, "rendered": text}


@router.patch("/{name}")
def toggle_prompt(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

"""Prompt Center API：模板注册（变量自动提取）/ 渲染 / 停用（docs/05 §3）。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import PromptRecord
from ...runtime.context import extract_prompt_variables, render_prompt
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/prompts",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class PromptCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = "1.0.0"
    description: str = ""
    template: str = Field(min_length=1)


class PromptRenderRequest(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict)
    key: str | None = Field(default=None, description="A/B 分流键（session_id/user_id）")


class VersionCreate(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    template: str = Field(min_length=1)
    notes: str = Field(default="", max_length=256)


class VersionPublish(BaseModel):
    version: str
    variables_sample: dict[str, str] = Field(default_factory=dict,
                                             description="发布前试渲染的样例变量")


class ExperimentCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    prompt: str
    version_a: str
    version_b: str
    percent_b: int = Field(default=50, ge=0, le=100)
    note: str = Field(default="", max_length=256)


@router.get("")
def list_prompts(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": p.name, "version": p.version, "description": p.description,
         "variables": p.variables, "enabled": p.enabled}
        for p in db.scalars(select(PromptRecord)).all()
    ]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def create_prompt(body: PromptCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(PromptRecord).where(PromptRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 Prompt {body.name} 已存在")
    from ...runtime import prompts as prompt_rt

    record = PromptRecord(
        name=body.name, version=body.version, description=body.description,
        template=body.template, variables=extract_prompt_variables(body.template),
    )
    db.add(record)
    # 初始版本同步进版本流水线（实验/回滚依赖版本记录）
    prompt_rt.create_version(db, body.name, body.version, body.template, "initial")
    prompt_rt.publish_version(db, body.name, body.version,
                              {v: "" for v in record.variables})
    db.commit()
    audit.record("prompt.create", actor=audit.actor_of(request), target=body.name,
                 detail={"version": body.version}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "variables": record.variables, "status": "registered"}


@router.post("/{name}/render")
def render(name: str, body: PromptRenderRequest, db: Session = fastapi.Depends(get_db)):
    """渲染（A/B 感知）：body.key 命中实验 B 桶时渲染 B 版本，返回实际命中的版本。"""
    from ...runtime import prompts as prompt_rt

    record = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    try:
        template, version, experiment = prompt_rt.resolve_template(db, name, body.key)
        text = render_prompt(template, body.variables)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    return {"name": name, "rendered": text, "version": version, "experiment": experiment}


@router.patch("/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_prompt(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}


# ---------- 版本流水线（draft→published→archived，支持回滚） ----------

@router.post("/{name}/versions", dependencies=[fastapi.Depends(require_admin)])
def create_version(name: str, body: VersionCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(PromptRecord).where(PromptRecord.name == name)) is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    from ...runtime import prompts as prompt_rt

    try:
        record = prompt_rt.create_version(db, name, body.version, body.template, body.notes)
        db.commit()
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=str(e)) from e
    return {"name": name, "version": record.version, "variables": record.variables,
            "state": record.state}


@router.get("/{name}/versions")
def list_versions(name: str, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(PromptRecord).where(PromptRecord.name == name)) is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {name} 不存在")
    from ...models import PromptVersionRecord

    rows = db.scalars(select(PromptVersionRecord).where(PromptVersionRecord.name == name)
                      .order_by(PromptVersionRecord.created_at.desc())).all()
    return [{"version": r.version, "state": r.state, "notes": r.notes,
             "variables": r.variables, "created_at": str(r.created_at)} for r in rows]


@router.post("/{name}/publish", dependencies=[fastapi.Depends(require_admin)])
def publish_version(name: str, body: VersionPublish, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    from ...runtime import prompts as prompt_rt

    try:
        record = prompt_rt.publish_version(db, name, body.version, body.variables_sample)
        db.commit()
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    audit.record("prompt.publish", actor=audit.actor_of(request), target=f"{name}@{body.version}",
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "version": record.version, "state": "published"}


@router.post("/{name}/rollback", dependencies=[fastapi.Depends(require_admin)])
def rollback_prompt(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    from ...runtime import prompts as prompt_rt

    try:
        record = prompt_rt.rollback_version(db, name)
        db.commit()
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    if record is None:
        raise fastapi.HTTPException(status_code=409,
                                    detail="EAP-6002 无可回滚的归档版本")
    audit.record("prompt.rollback", actor=audit.actor_of(request), target=name,
                 detail={"version": record.version}, trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "version": record.version, "state": "published"}


# ---------- A/B 实验 ----------

@router.get("/experiments")
def list_experiments(db: Session = fastapi.Depends(get_db)):
    from ...models import PromptExperimentRecord

    return [{"name": e.name, "prompt": e.prompt_name, "version_a": e.version_a,
             "version_b": e.version_b, "percent_b": e.percent_b, "enabled": e.enabled}
            for e in db.scalars(select(PromptExperimentRecord)).all()]


@router.post("/experiments", dependencies=[fastapi.Depends(require_admin)])
def create_experiment(body: ExperimentCreate, db: Session = fastapi.Depends(get_db)):
    from ...models import PromptExperimentRecord, PromptVersionRecord

    if db.scalar(select(PromptExperimentRecord).where(PromptExperimentRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 实验 {body.name} 已存在")
    if db.scalar(select(PromptRecord).where(PromptRecord.name == body.prompt)) is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 Prompt {body.prompt} 不存在")
    for v in (body.version_a, body.version_b):
        if db.scalar(select(PromptVersionRecord)
                     .where(PromptVersionRecord.name == body.prompt,
                            PromptVersionRecord.version == v)) is None:
            raise fastapi.HTTPException(status_code=404,
                                        detail=f"EAP-4004 Prompt {body.prompt}@{v} 不存在（需先创建版本）")
    record = PromptExperimentRecord(
        name=body.name, prompt_name=body.prompt, version_a=body.version_a,
        version_b=body.version_b, percent_b=body.percent_b, note=body.note)
    db.add(record)
    db.commit()
    return {"name": record.name, "prompt": record.prompt_name,
            "version_a": record.version_a, "version_b": record.version_b,
            "percent_b": record.percent_b}


@router.patch("/experiments/{name}", dependencies=[fastapi.Depends(require_admin)])
def toggle_experiment(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    from ...models import PromptExperimentRecord

    record = db.scalar(select(PromptExperimentRecord).where(PromptExperimentRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 实验 {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

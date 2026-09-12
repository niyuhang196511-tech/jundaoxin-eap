"""Agent 发布治理 API（docs/06 §2，M3）：版本生命周期 + 评测门禁 + 回滚。

状态机：draft → review → prod → rolled_back（历史版本提升时退役为 retired）。
门禁：review → prod 必须绑定一条 PASS 的规则裁判评测运行（docs/08 §3）。
"""

from __future__ import annotations

import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...models import AgentReleaseRecord
from ..deps import require_api_key, resolve_tenant
from .evals import execute_evaluation

router = fastapi.APIRouter(prefix="/api/v1/releases",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])

_EVAL_NOT_PASSED = "EAP-6001 评测门禁未通过：需先执行评测且结论为 PASS 才能发布到 prod"


class ReleaseCreate(BaseModel):
    agent: str
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+([-.\w]{0,32})?$")
    notes: str = Field(default="", max_length=256)


class EvalGateRequest(BaseModel):
    dataset: str
    min_pass_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    judge: str = Field(default="rule", pattern=r"^(rule|llm)$",
                       description="rule=关键词命中；llm=LLM-as-Judge")


def _view(r: AgentReleaseRecord) -> dict:
    return {"id": r.id, "agent": r.agent, "version": r.version, "state": r.state,
            "eval_run_id": r.eval_run_id, "eval_verdict": r.eval_verdict,
            "canary_percent": r.canary_percent, "overrides": r.overrides,
            "notes": r.notes, "updated_at": str(r.updated_at)}


def _get(db: Session, release_id: str) -> AgentReleaseRecord:
    record = db.get(AgentReleaseRecord, release_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 发布 {release_id} 不存在")
    return record


def _current_prod(db: Session, agent: str) -> AgentReleaseRecord | None:
    return db.scalar(select(AgentReleaseRecord)
                     .where(AgentReleaseRecord.agent == agent,
                            AgentReleaseRecord.state == "prod")
                     .order_by(AgentReleaseRecord.updated_at.desc()))


@router.post("")
def create_release(body: ReleaseCreate, db: Session = fastapi.Depends(get_db)):
    """登记一个发布草案；agent 必须已在 Agent Registry 纳管。"""
    if body.agent not in registry.names():
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 智能体 {body.agent} 未注册")
    exists = db.scalar(select(AgentReleaseRecord).where(
        AgentReleaseRecord.agent == body.agent, AgentReleaseRecord.version == body.version))
    if exists:
        raise fastapi.HTTPException(
            status_code=409, detail=f"EAP-2002 发布 {body.agent}@{body.version} 已存在")
    record = AgentReleaseRecord(id=uuid.uuid4().hex, agent=body.agent,
                                version=body.version, notes=body.notes)
    db.add(record)
    db.commit()
    return _view(record)


@router.get("")
def list_releases(agent: str | None = None, db: Session = fastapi.Depends(get_db)):
    stmt = select(AgentReleaseRecord).order_by(AgentReleaseRecord.created_at.desc())
    if agent:
        stmt = stmt.where(AgentReleaseRecord.agent == agent)
    return [_view(r) for r in db.scalars(stmt).all()]


@router.get("/current/{agent}")
def current_release(agent: str, db: Session = fastapi.Depends(get_db)):
    """当前 prod 版本；从未发布返回 null。"""
    record = _current_prod(db, agent)
    return _view(record) if record else None


@router.post("/{release_id}/eval")
async def run_gate(release_id: str, body: EvalGateRequest, db: Session = fastapi.Depends(get_db)):
    """对发布目标 agent 执行规则裁判评测，把运行结果绑定为该发布的门禁证据。"""
    record = _get(db, release_id)
    if record.state not in ("draft", "review"):
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-6002 状态 {record.state} 不允许执行门禁评测")
    result = await execute_evaluation(db, record.agent, body.dataset, body.min_pass_rate, body.judge)
    record.eval_run_id = result["run_id"]
    record.eval_verdict = result["verdict"]
    db.commit()
    return {**_view(record), "pass_rate": result["pass_rate"], "scores": result["scores"]}


@router.post("/{release_id}/promote")
def promote(release_id: str, db: Session = fastapi.Depends(get_db)):
    """推进状态：draft→review；review→prod（需评测 PASS 门禁，旧 prod 自动退役）；canary→prod。"""
    record = _get(db, release_id)
    if record.state == "draft":
        record.state = "review"
        db.commit()
        return _view(record)
    if record.state == "review":
        if record.eval_verdict != "PASS":
            raise fastapi.HTTPException(status_code=403, detail=_EVAL_NOT_PASSED)
        old = _current_prod(db, record.agent)
        record.state = "prod"
        if old and old.id != record.id:
            old.state = "retired"
        db.commit()
        return _view(record)
    if record.state == "canary":
        if record.eval_verdict != "PASS":
            raise fastapi.HTTPException(status_code=403, detail=_EVAL_NOT_PASSED)
        old = _current_prod(db, record.agent)
        record.state = "prod"
        record.canary_percent = 0
        if old and old.id != record.id:
            old.state = "retired"
        db.commit()
        return _view(record)
    raise fastapi.HTTPException(status_code=409, detail=f"EAP-6002 状态 {record.state} 不允许提升")


class CanarySet(BaseModel):
    percent: int = Field(ge=0, le=100)
    overrides: dict = Field(default_factory=dict,
                            description='灰度覆盖配置，MVP: {"model": "模型名"}')


@router.post("/{release_id}/canary")
def set_canary(release_id: str, body: CanarySet, db: Session = fastapi.Depends(get_db)):
    """进入/调整灰度：review→canary（需评测 PASS），设置流量百分比与覆盖配置。"""
    record = _get(db, release_id)
    if record.state == "canary":
        record.canary_percent = body.percent
        if body.overrides:
            record.overrides = body.overrides
        db.commit()
        return _view(record)
    if record.state == "review":
        if record.eval_verdict != "PASS":
            raise fastapi.HTTPException(status_code=403, detail=_EVAL_NOT_PASSED)
        record.state = "canary"
        record.canary_percent = body.percent
        record.overrides = body.overrides
        db.commit()
        return _view(record)
    raise fastapi.HTTPException(status_code=409, detail=f"EAP-6002 状态 {record.state} 不允许灰度")


@router.post("/{release_id}/rollback")
def rollback(release_id: str, db: Session = fastapi.Depends(get_db)):
    """回滚：prod 退役为 rolled_back 并恢复上一个 retired 版本；canary 直接下线。"""
    record = _get(db, release_id)
    if record.state not in ("prod", "canary"):
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-6002 仅 prod/canary 可回滚，当前 {record.state}")
    previous = None
    if record.state == "prod":
        previous = db.scalars(
            select(AgentReleaseRecord)
            .where(AgentReleaseRecord.agent == record.agent,
                   AgentReleaseRecord.state == "retired",
                   AgentReleaseRecord.eval_verdict == "PASS")
            .order_by(AgentReleaseRecord.updated_at.desc())).first()
    record.state = "rolled_back"
    record.canary_percent = 0
    if previous:
        previous.state = "prod"
    db.commit()
    return {"rolled_back": _view(record), "restored": _view(previous) if previous else None}

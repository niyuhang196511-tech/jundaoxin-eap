"""Policy Center API（docs/02 ①，M3）：租户模型策略的登记 / 启停。

策略在模型网关路由链上强制执行（runtime/policy.py）：
- model-allowlist：{"models": [...]}
- provider-allowlist：{"providers": [...]}（数据不出域）
- max-prompt-tokens：{"limit": N}
"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import PolicyRecord
from ...observability import audit as _audit
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/policies",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])

_KINDS = ("model-allowlist", "provider-allowlist", "max-prompt-tokens",
          "tool-allowlist", "tool-risk-approval", "agent-allowlist", "tool-sandbox")


class PolicyCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    tenant_id: int = Field(default=0, ge=0, description="0 = 平台默认策略")
    kind: str = Field(pattern=r"^(model-allowlist|provider-allowlist|max-prompt-tokens|"
                              r"tool-allowlist|tool-risk-approval|agent-allowlist|tool-sandbox)$")
    config: dict = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=1000)
    notes: str = Field(default="", max_length=256)


def _view(p: PolicyRecord) -> dict:
    return {"name": p.name, "tenant_id": p.tenant_id, "kind": p.kind,
            "config": p.config, "enabled": p.enabled, "priority": p.priority,
            "notes": p.notes}


@router.get("")
def list_policies(tenant_id: int | None = None, db: Session = fastapi.Depends(get_db)):
    stmt = select(PolicyRecord).order_by(PolicyRecord.priority, PolicyRecord.id)
    if tenant_id is not None:
        stmt = stmt.where(PolicyRecord.tenant_id == tenant_id)
    return [_view(p) for p in db.scalars(stmt).all()]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def create_policy(body: PolicyCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(PolicyRecord).where(PolicyRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 策略 {body.name} 已存在")
    cfg = body.config or {}
    if body.kind == "model-allowlist" and not isinstance(cfg.get("models"), list):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 model-allowlist 需要 config.models 列表")
    if body.kind == "tool-allowlist" and not isinstance(cfg.get("tools"), list):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 tool-allowlist 需要 config.tools 列表")
    if body.kind == "tool-risk-approval" and (cfg.get("threshold") or "high") not in ("low", "medium", "high"):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 tool-risk-approval 的 threshold 须为 low/medium/high")
    if body.kind == "agent-allowlist" and not isinstance(cfg.get("agents"), list):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 agent-allowlist 需要 config.agents 列表")
    if body.kind == "tool-sandbox":
        # M33 任务组 P2：{"mode": "enforce"|"audit", "tools": [...]}
        if not isinstance(cfg.get("tools"), list):
            raise fastapi.HTTPException(status_code=400, detail="EAP-7102 tool-sandbox 需要 config.tools 列表")
        if (cfg.get("mode") or "enforce") not in ("enforce", "audit"):
            raise fastapi.HTTPException(status_code=400, detail="EAP-7102 tool-sandbox 的 mode 须为 enforce/audit")
    if body.kind == "provider-allowlist" and not isinstance(cfg.get("providers"), list):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 provider-allowlist 需要 config.providers 列表")
    if body.kind == "max-prompt-tokens" and not isinstance(cfg.get("limit"), int):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7102 max-prompt-tokens 需要 config.limit 整数")
    if body.kind not in _KINDS:
        raise fastapi.HTTPException(status_code=400, detail=f"EAP-7102 未知策略类型 {body.kind}")
    record = PolicyRecord(name=body.name, tenant_id=body.tenant_id, kind=body.kind,
                          config=cfg, priority=body.priority, notes=body.notes)
    db.add(record)
    _audit.record("policy.create", actor=_audit.actor_of(request), target=body.name,
                  detail={"kind": body.kind, "config": cfg},
                  trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    return _view(record)


@router.post("/{name}/enabled", dependencies=[fastapi.Depends(require_admin)])
def toggle(name: str, enabled: bool, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(PolicyRecord).where(PolicyRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 策略 {name} 不存在")
    record.enabled = enabled
    _audit.record("policy.toggle", actor=_audit.actor_of(request), target=name,
                  detail={"enabled": enabled},
                  trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    return {"name": record.name, "enabled": record.enabled}

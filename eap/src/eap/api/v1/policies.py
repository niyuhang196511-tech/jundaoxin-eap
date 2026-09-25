"""Policy Center API（docs/02 ①，M3）：租户模型策略的登记 / 启停。

策略在模型网关路由链上强制执行（runtime/policy.py）：
- model-allowlist：{"models": [...]}
- provider-allowlist：{"providers": [...]}（数据不出域）
- max-prompt-tokens：{"limit": N}
- eval-gate（M42-B，docs/10 评测门禁接入模型路由）：
  {"models": [...], "require_eval": bool, "min_pass_rate": 0.8}
- env-protection（M55-E，工作流环境保护）：{"rules": [{"env": "prod",
  "allowed_actors": [...], "require_confirm": bool}]}
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
          "tool-allowlist", "tool-risk-approval", "agent-allowlist", "tool-sandbox",
          "eval-gate", "a2a-delegate-allowlist", "env-protection")

# 环境保护规则的合法 env（M55-E）：与 runtime/workflow_versions.ENVS 对齐
_ENV_PROTECTION_ENVS = ("dev", "test", "staging", "prod")


def _validate_env_protection_config(cfg: dict) -> None:
    """env-protection config 校验：rules 必填且逐条核验 env/actors/confirm 形状。"""
    rules = cfg.get("rules")
    if not isinstance(rules, list) or not rules:
        raise fastapi.HTTPException(
            status_code=400, detail="EAP-7102 env-protection 需要 config.rules 非空列表")
    for rule in rules:
        if not isinstance(rule, dict):
            raise fastapi.HTTPException(
                status_code=400, detail="EAP-7102 env-protection 的 rules 项须为对象")
        if rule.get("env") not in _ENV_PROTECTION_ENVS:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"EAP-7102 env-protection 规则的 env 须为 {'/'.join(_ENV_PROTECTION_ENVS)} 之一")
        actors = rule.get("allowed_actors")
        if not isinstance(actors, list) or any(not isinstance(a, str) for a in actors):
            raise fastapi.HTTPException(
                status_code=400,
                detail="EAP-7102 env-protection 规则的 allowed_actors 须为字符串数组"
                       "（audit.actor_of 形态：jwt:<user> / api-key；空名单 = 冻结该环境）")
        if "require_confirm" in rule and not isinstance(rule["require_confirm"], bool):
            raise fastapi.HTTPException(
                status_code=400, detail="EAP-7102 env-protection 规则的 require_confirm 须为布尔")


class PolicyCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    tenant_id: int = Field(default=0, ge=0, description="0 = 平台默认策略")
    kind: str = Field(pattern=r"^(model-allowlist|provider-allowlist|max-prompt-tokens|"
                              r"tool-allowlist|tool-risk-approval|agent-allowlist|tool-sandbox|"
                              r"eval-gate|a2a-delegate-allowlist|env-protection)$")
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
    if body.kind == "eval-gate":
        # M42-B：{"models": [...], "require_eval": bool, "min_pass_rate": 0~1}
        if not isinstance(cfg.get("models"), list):
            raise fastapi.HTTPException(status_code=400, detail="EAP-7102 eval-gate 需要 config.models 列表")
        if "require_eval" in cfg and not isinstance(cfg["require_eval"], bool):
            raise fastapi.HTTPException(status_code=400, detail="EAP-7102 eval-gate 的 require_eval 须为布尔")
        min_rate = cfg.get("min_pass_rate")
        if min_rate is not None and (not isinstance(min_rate, (int, float)) or not 0 <= float(min_rate) <= 1):
            raise fastapi.HTTPException(status_code=400, detail="EAP-7102 eval-gate 的 min_pass_rate 须为 0~1 数值")
    if body.kind == "a2a-delegate-allowlist":
        # M30 任务组 E（M46-C 补 API 缺口）：{"endpoints": [...], "agents": [...]}
        # 至少一项列表（endpoint 或 agent 命中即放行，"*" 通配，见 check_a2a_delegate）
        if not isinstance(cfg.get("endpoints"), list) and not isinstance(cfg.get("agents"), list):
            raise fastapi.HTTPException(
                status_code=400, detail="EAP-7102 a2a-delegate-allowlist 需要 config.endpoints 或 config.agents 列表")
    if body.kind == "env-protection":
        # M55-E 工作流环境保护：{"rules": [{"env", "allowed_actors", "require_confirm"}]}
        _validate_env_protection_config(cfg)
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

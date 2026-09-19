"""成本中心 API（docs/08 §4）：预算设置 / 用量汇总 / 熔断状态。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sqlalchemy import select

from ...db import get_db
from ...runtime import budget
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/budgets",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class BudgetSet(BaseModel):
    tenant_id: int = Field(ge=1)
    monthly_token_budget: int = Field(ge=0, description="0 = 不限")
    enabled: bool = True
    note: str = Field(default="", max_length=128)


@router.put("", dependencies=[fastapi.Depends(require_admin)])
def upsert_budget(body: BudgetSet, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    from ...observability import audit

    record = budget.set_budget(db, body.tenant_id, body.monthly_token_budget,
                               enabled=body.enabled, note=body.note)
    db.commit()
    audit.record("budget.set", actor=audit.actor_of(request), target=str(body.tenant_id),
                 detail={"monthly_token_budget": body.monthly_token_budget, "enabled": body.enabled},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"tenant_id": record.tenant_id, "monthly_token_budget": record.monthly_token_budget,
            "enabled": record.enabled, "note": record.note}


@router.get("/{tenant_id}")
def get_budget(tenant_id: int, db: Session = fastapi.Depends(get_db)):
    """预算 + 当月用量 + 熔断状态一览。"""
    record = budget.get_budget(db, tenant_id)
    state = budget.check_budget(db, tenant_id)
    usage = budget.month_usage(db, tenant_id)
    return {
        "tenant_id": tenant_id,
        "monthly_token_budget": record.monthly_token_budget if record else 0,
        "enabled": record.enabled if record else False,
        "blocked": state["blocked"],
        "usage": usage,
    }


@router.get("/{tenant_id}/summary")
def usage_summary(tenant_id: int, db: Session = fastapi.Depends(get_db)):
    """当月计量明细（按 kind / model 分组）。"""
    return budget.month_usage(db, tenant_id)


@router.get("/{tenant_id}/report")
def cost_report(tenant_id: int, days: int = 30, db: Session = fastapi.Depends(get_db)):
    """成本报表（v0.6-⑤）：按模型 / 智能体 / 日的 SQL 聚合（金额单位与定价一致）。"""
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from ...models import UsageRecord

    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    base = (select(UsageRecord)
            .where(UsageRecord.tenant_id == tenant_id,
                   UsageRecord.created_at >= since))

    by_model = db.execute(
        select(UsageRecord.model,
               func.count(UsageRecord.id),
               func.sum(UsageRecord.tokens_in),
               func.sum(UsageRecord.tokens_out),
               func.sum(UsageRecord.cost))
        .where(UsageRecord.tenant_id == tenant_id,
               UsageRecord.created_at >= since)
        .group_by(UsageRecord.model)).all()
    by_agent = db.execute(
        select(UsageRecord.agent,
               func.count(UsageRecord.id),
               func.sum(UsageRecord.tokens_in),
               func.sum(UsageRecord.tokens_out),
               func.sum(UsageRecord.cost))
        .where(UsageRecord.tenant_id == tenant_id,
               UsageRecord.created_at >= since)
        .group_by(UsageRecord.agent)).all()
    by_day = db.execute(
        select(func.date(UsageRecord.created_at),
               func.sum(UsageRecord.cost))
        .where(UsageRecord.tenant_id == tenant_id,
               UsageRecord.created_at >= since)
        .group_by(func.date(UsageRecord.created_at))
        .order_by(func.date(UsageRecord.created_at))).all()
    total = db.execute(
        select(func.coalesce(func.sum(UsageRecord.cost), 0.0))
        .where(UsageRecord.tenant_id == tenant_id,
               UsageRecord.created_at >= since)).scalar()
    _ = base  # 保留 since 语义说明
    return {
        "tenant_id": tenant_id, "days": days, "total_cost": round(float(total or 0.0), 6),
        "by_model": [{"model": m or "", "calls": c,
                      "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                      "cost": round(float(cost or 0.0), 6)} for m, c, ti, to, cost in by_model],
        "by_agent": [{"agent": a or "", "calls": c,
                      "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                      "cost": round(float(cost or 0.0), 6)} for a, c, ti, to, cost in by_agent],
        "by_day": [{"date": str(d), "cost": round(float(cost or 0.0), 6)} for d, cost in by_day],
    }

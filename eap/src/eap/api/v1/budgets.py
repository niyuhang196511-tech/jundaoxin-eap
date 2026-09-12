"""成本中心 API（docs/08 §4）：预算设置 / 用量汇总 / 熔断状态。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db import get_db
from ...runtime import budget
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/budgets",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class BudgetSet(BaseModel):
    tenant_id: int = Field(ge=1)
    monthly_token_budget: int = Field(ge=0, description="0 = 不限")
    enabled: bool = True
    note: str = Field(default="", max_length=128)


@router.put("")
def upsert_budget(body: BudgetSet, db: Session = fastapi.Depends(get_db)):
    record = budget.set_budget(db, body.tenant_id, body.monthly_token_budget,
                               enabled=body.enabled, note=body.note)
    db.commit()
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

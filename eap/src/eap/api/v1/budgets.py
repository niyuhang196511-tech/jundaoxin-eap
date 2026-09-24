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
    return _report_payload(db, tenant_id, days)


def _report_payload(db: Session, tenant_id: int, days: int) -> dict:
    """报表聚合（report 查询与 export 导出共用，M48-A）：since 语义 days 夹取 [1, 365]。"""
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from ...models import UsageRecord

    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
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


@router.get("/report/export", dependencies=[fastapi.Depends(require_admin)])
def export_report(tenant_id: int, days: int = 30, request: fastapi.Request = None,
                  db: Session = fastapi.Depends(get_db)):
    """成本报表导出（M48-A，#25）：admin 专用 CSV 附件下载，聚合逻辑与过滤参数（tenant_id/days）
    与 GET /{tenant_id}/report 完全一致（路径用固定前缀 report/export，避开 /{tenant_id} 通配）。

    - 平面 CSV（UTF-8 BOM，Excel 直开中文不乱码）：section(model/agent/day/total) + dimension +
      calls/tokens_in/tokens_out/cost；day 行无 token 维度留空，total 行汇总总成本；
    - 行数可控（days≤365 × 模型/智能体基数）直接拼装，不流式；
    - 导出动作自身落审计 budget.export（仅条数与过滤条件，不含导出内容，对齐 audit.export）。
    """
    import csv
    import io
    from datetime import datetime, timezone

    from ...observability import audit

    report = _report_payload(db, tenant_id, days)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "dimension", "calls", "tokens_in", "tokens_out", "cost"])
    for r in report["by_model"]:
        writer.writerow(["model", r["model"], r["calls"], r["tokens_in"], r["tokens_out"],
                         f"{r['cost']:.6f}"])
    for r in report["by_agent"]:
        writer.writerow(["agent", r["agent"], r["calls"], r["tokens_in"], r["tokens_out"],
                         f"{r['cost']:.6f}"])
    for r in report["by_day"]:
        writer.writerow(["day", r["date"], "", "", "", f"{r['cost']:.6f}"])
    writer.writerow(["total", "all", "", "", "", f"{report['total_cost']:.6f}"])
    rows = len(report["by_model"]) + len(report["by_agent"]) + len(report["by_day"]) + 1
    audit.record("budget.export", actor=audit.actor_of(request), target=str(tenant_id),
                 detail={"days": report["days"], "rows": rows},
                 trace_id=getattr(request.state, "trace_id", ""))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return fastapi.responses.Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="budget-report-tenant{tenant_id}-{stamp}.csv"'},
    )

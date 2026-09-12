"""成本中心·预算服务（docs/08 §4）：自然月 token 用量汇总 + 超限熔断。

计量事实来自 TraceMiddleware 的 record_usage（chat/agent/embed 三类）；
预算按租户×自然月，月初清零（月切分按 UTC）。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BudgetRecord, UsageRecord


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def month_label(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year}-{calendar.month_abbr[now.month]}"


def month_usage(db: Session, tenant_id: int) -> dict:
    """当月计量汇总：调用次数 / token 进出 / 按类别与模型分组。"""
    start = month_start()
    rows = db.scalars(
        select(UsageRecord)
        .where(UsageRecord.tenant_id == tenant_id, UsageRecord.created_at >= start)
    ).all()
    by_kind: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0, "latency_ms": 0}
    for r in rows:
        for bucket, key in ((by_kind, r.kind), (by_model, r.model or "unknown")):
            b = bucket.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            b["calls"] += 1
            b["tokens_in"] += r.tokens_in
            b["tokens_out"] += r.tokens_out
        total["calls"] += 1
        total["tokens_in"] += r.tokens_in
        total["tokens_out"] += r.tokens_out
        total["latency_ms"] += r.latency_ms
    total["tokens_total"] = total["tokens_in"] + total["tokens_out"]
    return {"month": month_label(), "tenant_id": tenant_id, **total,
            "by_kind": by_kind, "by_model": by_model}


def get_budget(db: Session, tenant_id: int) -> BudgetRecord:
    return db.scalar(select(BudgetRecord).where(BudgetRecord.tenant_id == tenant_id))


def set_budget(db: Session, tenant_id: int, monthly_token_budget: int,
               enabled: bool = True, note: str = "") -> BudgetRecord:
    record = get_budget(db, tenant_id)
    if record is None:
        record = BudgetRecord(tenant_id=tenant_id)
        db.add(record)
    record.monthly_token_budget = max(0, monthly_token_budget)
    record.enabled = enabled
    record.note = note
    db.flush()
    return record


def check_budget(db: Session, tenant_id: int) -> dict:
    """返回 {blocked, reason, budget, used, remaining}；未设预算或禁用则不拦。"""
    usage = month_usage(db, tenant_id)
    record = get_budget(db, tenant_id)
    budget = record.monthly_token_budget if record and record.enabled else 0
    if not budget:  # 未设置或已禁用 → 不限
        return {"blocked": False, "budget": 0, **{k: usage[k] for k in ("month", "tokens_total")}}
    used = usage["tokens_total"]
    remaining = max(0, budget - used)
    return {"blocked": used >= budget, "budget": budget, "used": used,
            "remaining": remaining, "month": usage["month"]}


def guard(db: Session, tenant_id: int) -> None:
    """调用侧熔断：预算超限 → 429（docs/08 §4）。"""
    state = check_budget(db, tenant_id)
    if state["blocked"]:
        raise RuntimeError(
            f"EAP-7001 租户 {tenant_id} 本月 token 预算已用尽"
            f"（{state['used']}/{state['budget']}），请提升预算或下月重试")

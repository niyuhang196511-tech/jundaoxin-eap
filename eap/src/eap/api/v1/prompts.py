"""Prompt Center API：模板注册（变量自动提取）/ 渲染 / 停用（docs/05 §3）。

L10/M34：A/B 实验效果报表（GET /experiments/{name}/report）与结论辅助
（POST /experiments/{name}/conclude，见文件尾部）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# ---------- A/B 效果报表与结论辅助（L10/M34） ----------

class ExperimentConclude(BaseModel):
    winner: str = Field(pattern=r"^(a|b)$", description="胜出 variant：a | b")
    reason: str = Field(default="", max_length=512, description="结论理由（落审计）")
    promote: bool = Field(
        default=False,
        description="是否把胜出版本经版本流水线 publish 为当前发布版（复用 prompt.publish 校验）")


def _p50(values: list) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _variant_metrics(version: str | None, render_rows: list, run_rows: list,
                     db: Session) -> dict:
    """单 variant 聚合：渲染数（精确）+ 归因调用指标（近似，口径见 attribution）。

    run_rows 为该 variant 已归因的 agent.run.completed 审计行；
    token/成本按归因调用的 trace_id 关联 usage_records 全部计量行求和。
    """
    from ...models import UsageRecord

    traces = {r.trace_id for r in run_rows if r.trace_id}
    usage_rows = (db.scalars(select(UsageRecord)
                             .where(UsageRecord.trace_id.in_(traces))).all()
                  if traces else [])
    latencies = [(r.detail or {}).get("elapsed_ms") for r in run_rows]
    latencies = [v for v in latencies if isinstance(v, (int, float))]
    ok = sum(1 for r in run_rows if (r.detail or {}).get("status") == "ok")
    errors = len(run_rows) - ok
    return {
        "version": version,
        "renders": len(render_rows),
        "attributed_invocations": len(run_rows),
        "success": {"ok": ok, "error": errors,
                    "rate": round(ok / len(run_rows), 4) if run_rows else None},
        "tokens_in": sum(u.tokens_in for u in usage_rows),
        "tokens_out": sum(u.tokens_out for u in usage_rows),
        "cost": round(sum(u.cost for u in usage_rows), 6),
        "latency_ms": {"p50": _p50(latencies),
                       "mean": round(sum(latencies) / len(latencies), 1) if latencies else None},
    }


@router.get("/experiments/{name}/report", dependencies=[fastapi.Depends(require_admin)])
def experiment_report(name: str, request: fastapi.Request, since_hours: float | None = None,
                      db: Session = fastapi.Depends(get_db)):
    """A/B 实验效果报表：分 variant 聚合渲染/调用/成功率/token/成本/延迟。

    归因口径（响应 attribution 字段同步返回，诚实标注精确与近似）：
    - renders 精确：审计 prompt.render 按 version_selected 计数（管理面 render 端点
      与智能体内部 ctx.prompt 的实验命中渲染都落此审计）；
    - 调用/成功率/token/成本/延迟近似：仅覆盖「同步智能体调用内部渲染了本实验
      prompt」的调用（agent.run.completed 审计携带 prompt_variant；SSE 流式路径
      暂不落该审计，不计入），token/成本按 trace_id 关联 usage_records；
    - 归因不到的维度（如某 variant 无归因调用）返回 0 或 null，不编造数据。
    """
    from ...models import AuditLog, PromptExperimentRecord

    exp = db.scalar(select(PromptExperimentRecord).where(PromptExperimentRecord.name == name))
    if exp is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 实验 {name} 不存在")
    if since_hours is not None and since_hours < 0:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 since_hours 须 >= 0")

    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)
              if since_hours is not None else None)
    in_window = lambda row: cutoff is None or row.created_at >= cutoff  # noqa: E731

    render_rows = [r for r in db.scalars(select(AuditLog)
                                         .where(AuditLog.action == "prompt.render")).all()
                   if in_window(r) and (r.detail or {}).get("experiment") == name]
    run_rows_all = [r for r in db.scalars(select(AuditLog)
                                          .where(AuditLog.action == "agent.run.completed")).all()
                    if in_window(r)]
    # 按 prompt_variant 归因：experiment 命中本实验即归入对应 variant（fallback 单列）
    variant_of = {"a": [], "b": [], "fallback": []}
    render_of = {"a": [], "b": [], "fallback": []}
    for r in render_rows:
        picked = (r.detail or {}).get("picked")
        render_of[picked if picked in render_of else "a"].append(r)
    for r in run_rows_all:
        variant = (r.detail or {}).get("prompt_variant") or {}
        if variant.get("experiment") != name:
            continue
        picked = variant.get("picked")
        variant_of[picked if picked in variant_of else "a"].append(r)

    metrics = {
        "a": _variant_metrics(exp.version_a, render_of["a"], variant_of["a"], db),
        "b": _variant_metrics(exp.version_b, render_of["b"], variant_of["b"], db),
        "fallback": _variant_metrics(None, render_of["fallback"], variant_of["fallback"], db),
    }
    classified = metrics["a"]["renders"] + metrics["b"]["renders"]
    observed_b_share = (round(metrics["b"]["renders"] / classified * 100, 1)
                        if classified else None)
    audit.record("prompt.ab.report", actor=audit.actor_of(request), target=name,
                 detail={"since_hours": since_hours, "observed_b_share": observed_b_share},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {
        "experiment": name, "prompt": exp.prompt_name,
        "version_a": exp.version_a, "version_b": exp.version_b,
        "percent_b": exp.percent_b, "enabled": exp.enabled,
        "window_hours": since_hours,
        "split": {"classified_renders": classified, "observed_b_share": observed_b_share,
                  "percent_b_config": exp.percent_b,
                  "deviation": (round(observed_b_share - exp.percent_b, 1)
                                if observed_b_share is not None else None)},
        "variants": metrics,
        "attribution": {
            "renders": "精确：审计 prompt.render 按 version_selected 计数（管理面 render 端点 + 智能体内部 ctx.prompt 的实验命中渲染）",
            "invocations": "近似：仅统计同步智能体调用内部渲染了本实验 prompt 的调用（agent.run.completed 审计携带 prompt_variant）；SSE 流式路径暂不落该审计，不计入；管理面渲染只计入 renders",
            "success_rate": "近似：ok/(ok+error)，来自同一审计的状态字段；渲染前失败（如策略拒绝/404）无变体可归因，不计入",
            "tokens_cost": "近似：归因调用 trace_id 关联 usage_records 全部计量行求和（含该次调用的所有模型调用）",
            "latency_ms": "近似：归因调用 agent.run.completed 的 elapsed_ms（端到端，含 ok 与 error）",
            "multi_experiment": "一次调用渲染多个实验 prompt 时按最近一次命中归因（variant_scope 覆盖语义）",
            "nulls": "无归因数据的维度返回 0 或 null，不编造",
        },
    }


@router.post("/experiments/{name}/conclude", dependencies=[fastapi.Depends(require_admin)])
def conclude_experiment(name: str, body: ExperimentConclude, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    """结束实验：记录胜出 variant + 理由 → 实验禁用；promote=true 时把胜出版本
    经版本流水线 publish 为当前发布版（复用 prompt.publish 校验，失败则整体拒绝）。
    审计 prompt.ab.conclude。"""
    from ...models import PromptExperimentRecord
    from ...runtime import prompts as prompt_rt

    exp = db.scalar(select(PromptExperimentRecord).where(PromptExperimentRecord.name == name))
    if exp is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 实验 {name} 不存在")
    prompt = db.scalar(select(PromptRecord).where(PromptRecord.name == exp.prompt_name))
    if prompt is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 Prompt {exp.prompt_name} 不存在")
    winner_version = exp.version_a if body.winner == "a" else exp.version_b
    promoted = False
    if body.promote:
        try:
            prompt_rt.publish_version(db, exp.prompt_name, winner_version,
                                      {v: "" for v in (prompt.variables or [])})
        except KeyError as e:
            raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
        promoted = True
    exp.enabled = False
    db.commit()
    audit.record("prompt.ab.conclude", actor=audit.actor_of(request), target=name,
                 detail={"winner": body.winner, "version": winner_version,
                         "reason": body.reason, "promoted": promoted},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "prompt": exp.prompt_name, "winner": body.winner,
            "version": winner_version, "promoted": promoted,
            "enabled": exp.enabled, "state": "concluded"}

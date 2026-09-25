"""Workflow API：DSL 创建 → 动态注册为智能体 / 目录 / 停用 / 试运行（DSL v2 运行记录）。"""

from __future__ import annotations

import asyncio

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import WorkflowRecord, WorkflowRunRecord, WorkflowVersionRecord
from ...runtime import workflow_versions
from ...runtime.workflow import WorkflowSpec
from ...workflows import (create_and_register, disable, load_enabled, refresh_registration,
                          resume_run_async, test_run_async)
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/workflows",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_workflows(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": w.name, "version": w.version, "enabled": w.enabled,
         "steps": len((w.dsl or {}).get("steps", [])),
         "edges": len((w.dsl or {}).get("edges", []))}
        for w in db.scalars(select(WorkflowRecord)).all()
    ]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
async def create_workflow(body: WorkflowSpec, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """DSL 创建即注册：工作流立刻成为可调用、可嵌入的智能体。"""
    try:
        record = await create_and_register(db, body)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 {e}") from e
    audit.record("workflow.create", actor=audit.actor_of(request), target=record.name,
                 detail={"steps": len(body.steps), "edges": len(body.edges)},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "version": record.version, "steps": len(body.steps),
            "invoke": f"/api/v1/agents/{record.name}/invocations"}


@router.get("/{name}/dsl")
def get_workflow_dsl(name: str, db: Session = fastapi.Depends(get_db)):
    """DSL 全文（画布编辑器数据源）。"""
    record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 工作流 {name} 不存在")
    return record.dsl or {}


@router.post("/{name}/test-run")
async def test_run(name: str, body: dict):
    """后台执行一次试运行，立即返回 run_id（逐节点执行历史落 workflow_runs）。

    M32：body 可传 env（dev|test|staging|prod）或 version（版本号）覆盖本次试运行
    的 DSL 解析（测试不落版本记录）；缺省走执行解析链（prod 指针 → dsl 兜底）。
    """
    env = body.get("env")
    version = body.get("version")
    try:
        if version is not None:
            version = int(version)
        run_id = await test_run_async(name, str(body.get("input", "")),
                                      env=env, version=version)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    return {"run_id": run_id}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = fastapi.Depends(get_db)):
    """运行详情（前端 500ms 轮询驱动节点状态环）。"""
    record = db.get(WorkflowRunRecord, run_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 运行 {run_id} 不存在")
    pending = None
    if record.status == "waiting_input":
        # 交互引擎（v0.5-⑤）：挂起运行的交互信息（复用 interactions 表）
        from ...models import InteractionRecord

        itx = db.scalar(select(InteractionRecord)
                        .where(InteractionRecord.run_id == run_id,
                               InteractionRecord.state == "waiting")
                        .order_by(InteractionRecord.created_at.desc()))
        if itx is not None:
            pending = {"interaction_id": itx.id, "ui_schema": itx.schema}
    return {
        "id": record.id, "workflow": record.workflow, "version": record.version,
        "input": record.input, "output": record.output, "status": record.status,
        "error": record.error, "node_runs": record.node_runs or [],
        "elapsed_ms": record.elapsed_ms, "created_at": str(record.created_at),
        "pending_interaction": pending,
    }


class RunSubmit(BaseModel):
    values: dict = Field(min_length=1, description="用户提交的交互表单值")


@router.post("/runs/{run_id}/submit")
async def submit_run(run_id: str, body: RunSubmit, db: Session = fastapi.Depends(get_db)):
    """交互提交（v0.5-⑤）：恢复变量快照，从挂起节点继续执行（后台任务）。"""
    from ...models import InteractionRecord

    record = db.get(WorkflowRunRecord, run_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 运行 {run_id} 不存在")
    if record.status != "waiting_input":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-4006 运行 {run_id} 状态为 {record.status}，不在等待输入")
    itx = db.scalar(select(InteractionRecord)
                    .where(InteractionRecord.run_id == run_id,
                           InteractionRecord.state == "waiting")
                    .order_by(InteractionRecord.created_at.desc()))
    if itx is not None:
        itx.values = body.values
        itx.state = "submitted"
        db.commit()
    asyncio.get_running_loop().create_task(resume_run_async(record.workflow, run_id, body.values))
    return {"run_id": run_id, "status": "running"}


@router.get("/{name}/runs")
def list_runs(name: str, db: Session = fastapi.Depends(get_db)):
    """工作流历史运行列表（新→旧，截断 50 条）。"""
    records = db.scalars(
        select(WorkflowRunRecord).where(WorkflowRunRecord.workflow == name)
        .order_by(WorkflowRunRecord.created_at.desc()).limit(50)
    ).all()
    return [
        {"id": r.id, "status": r.status, "input": r.input, "output": r.output,
         "error": r.error, "elapsed_ms": r.elapsed_ms, "created_at": str(r.created_at)}
        for r in records
    ]


# ---------- 版本化 + prod-env- 环境体系（M32，风格对齐 agents 配置版本端点） ----------

class VersionDraftBody(BaseModel):
    note: str = Field(default="", max_length=256)


class VersionPublishBody(BaseModel):
    env: str = Field(pattern=r"^(dev|test|staging|prod)$",
                     description="发布目标环境：dev|test|staging|prod")


class VersionRollbackBody(BaseModel):
    env: str = Field(pattern=r"^(dev|test|staging|prod)$",
                     description="回滚目标环境：回滚该 env 到上一版")


def _require_workflow(db: Session, name: str) -> WorkflowRecord:
    record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 工作流 {name} 不存在")
    return record


@router.get("/{name}/versions")
def list_versions(name: str, db: Session = fastapi.Depends(get_db)):
    """版本列表 + 当前生产指针（workflow-as-agent 的生效版本）。"""
    record = _require_workflow(db, name)
    rows = db.scalars(select(WorkflowVersionRecord)
                      .where(WorkflowVersionRecord.workflow_id == record.id)
                      .order_by(WorkflowVersionRecord.version.desc())).all()
    return {
        "workflow": name,
        "published_version_id": record.published_version_id,
        "versions": [
            {"id": r.id, "version": r.version, "env": r.env, "state": r.state,
             "note": r.note, "created_at": str(r.created_at),
             "published_at": str(r.published_at) if r.published_at else None}
            for r in rows
        ],
    }


@router.post("/{name}/versions", dependencies=[fastapi.Depends(require_admin)])
def save_workflow_draft(name: str, body: VersionDraftBody, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    """从当前 dsl 存草稿（version = max+1 递增）。"""
    record = _require_workflow(db, name)
    try:
        row = workflow_versions.save_draft(db, record, record.dsl or {}, body.note)
        db.commit()
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    audit.record("workflow.version.create", actor=audit.actor_of(request),
                 target=f"{name}@v{row.version}", detail={"note": body.note},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"workflow": name, "id": row.id, "version": row.version, "state": row.state}


@router.get("/{name}/versions/{version_id}")
def get_workflow_version(name: str, version_id: int, db: Session = fastapi.Depends(get_db)):
    """单版本详情（含 DSL 全文，画布环境预览数据源）。"""
    record = _require_workflow(db, name)
    row = db.scalar(select(WorkflowVersionRecord)
                    .where(WorkflowVersionRecord.workflow_id == record.id,
                           WorkflowVersionRecord.id == version_id))
    if row is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 工作流 {name} 版本 {version_id} 不存在")
    return {"workflow": name, "id": row.id, "version": row.version, "env": row.env,
            "state": row.state, "note": row.note, "dsl": row.dsl or {},
            "created_at": str(row.created_at),
            "published_at": str(row.published_at) if row.published_at else None}


@router.post("/{name}/versions/{version_id}/publish", dependencies=[fastapi.Depends(require_admin)])
async def publish_workflow_version(name: str, version_id: int, body: VersionPublishBody,
                                   request: fastapi.Request,
                                   db: Session = fastapi.Depends(get_db)):
    """发布到环境：DSL 校验 → 同 env 旧版归档；env=prod 同步生产指针并热更新注册。"""
    record = _require_workflow(db, name)
    try:
        row = workflow_versions.publish(db, record, version_id, body.env)
        db.commit()
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    audit.record("workflow.version.publish", actor=audit.actor_of(request),
                 target=f"{name}@v{row.version}", detail={"env": body.env},
                 trace_id=getattr(request.state, "trace_id", ""))
    await refresh_registration(name)  # 热更新：workflow-as-agent 立即用新版
    return {"workflow": name, "id": row.id, "version": row.version,
            "env": row.env, "state": row.state}


@router.post("/{name}/versions/{version_id}/rollback", dependencies=[fastapi.Depends(require_admin)])
async def rollback_workflow_version(name: str, version_id: int, body: VersionRollbackBody,
                                    request: fastapi.Request,
                                    db: Session = fastapi.Depends(get_db)):
    """回滚该环境到上一版：现 published → archived，最近 archived → published（prod 同步指针）。"""
    record = _require_workflow(db, name)
    target = db.get(WorkflowVersionRecord, version_id)
    if target is None or target.workflow_id != record.id:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 工作流 {name} 版本 {version_id} 不存在")
    try:
        row = workflow_versions.rollback(db, record, body.env)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e)) from e
    if row is None:
        db.rollback()  # 丢弃 flush：无可回滚版本时现状不变
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-6002 环境 {body.env} 无更早的可回滚版本")
    db.commit()
    audit.record("workflow.version.rollback", actor=audit.actor_of(request),
                 target=f"{name}@v{row.version}", detail={"env": body.env,
                                                          "from_version": target.version},
                 trace_id=getattr(request.state, "trace_id", ""))
    await refresh_registration(name)  # 热更新：生产指针变化后 workflow-as-agent 立即切回
    return {"workflow": name, "id": row.id, "version": row.version,
            "env": row.env, "state": row.state}


@router.get("/{name}/versions/{version_a}/diff/{version_b}")
def diff_workflow_versions(name: str, version_a: int, version_b: int,
                           db: Session = fastapi.Depends(get_db)):
    """两个版本的 DSL 结构化差异（仅报告不同项，形态对齐 agent diff 端点）。"""
    record = _require_workflow(db, name)
    rows = {r.id: r for r in db.scalars(select(WorkflowVersionRecord)
            .where(WorkflowVersionRecord.workflow_id == record.id,
                   WorkflowVersionRecord.id.in_([version_a, version_b]))).all()}
    for vid in (version_a, version_b):
        if vid not in rows:
            raise fastapi.HTTPException(status_code=404,
                                        detail=f"EAP-4004 工作流 {name} 版本 {vid} 不存在")
    changes = workflow_versions.diff_dsl(rows[version_a].dsl or {}, rows[version_b].dsl or {})
    return {"workflow": name, "from": version_a, "to": version_b, "changes": changes}


def _version_meta(row: WorkflowVersionRecord) -> dict:
    """版本元信息视图（diff 两侧各带一份：version 号/created_at/说明/state）。"""
    return {"id": row.id, "version": row.version, "state": row.state, "note": row.note,
            "created_at": str(row.created_at)}


@router.get("/{name}/versions/{version_id}/diff")
def diff_workflow_version_against(name: str, version_id: int, against: str,
                                  db: Session = fastapi.Depends(get_db)):
    """版本与另一版本或当前草稿的 DSL 结构化差异（M54-B，画布版本对比数据源）。

    - against 取值："draft" = 与 WorkflowRecord.dsl（画布当前保存的 DSL）比较，to 侧无版本
      元信息（to_meta=null）；数字 = 另一版本记录 id（与 GET /{name}/versions 列表 id 同义）
    - 方向语义：from = {version_id}，to = against；changes 仅报告不同项（形态对齐既有
      diff/{version_b} 端点与 agent diff 端点）：steps/edges 按节点/边 id 对齐，
      逐字段比较 type/system/prompt_name/tool_name/ui_schema 等全部字段
    - 鉴权对齐既有只读版本端点（版本列表/详情/diff 均无需 admin，仅 router 级租户+API key）
    - 只读端点不落审计（平台惯例：仅写操作记审计，与 GET /{name}/versions 一致）
    - 不做语义级移动检测：同 id 的步骤/边任一字段不同即记「修改」；节点位移（position
      变化）属普通字段差异，不作特殊判定
    """
    record = _require_workflow(db, name)
    row = db.scalar(select(WorkflowVersionRecord)
                    .where(WorkflowVersionRecord.workflow_id == record.id,
                           WorkflowVersionRecord.id == version_id))
    if row is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 工作流 {name} 版本 {version_id} 不存在")
    if against == "draft":
        to_dsl = record.dsl or {}
        other_id: int | str = "draft"
        to_meta = None
    else:
        try:
            other_id = int(against)
        except ValueError:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"EAP-4000 against 需为版本 id 或 draft，收到 {against!r}") from None
        other = db.scalar(select(WorkflowVersionRecord)
                          .where(WorkflowVersionRecord.workflow_id == record.id,
                                 WorkflowVersionRecord.id == other_id))
        if other is None:
            raise fastapi.HTTPException(status_code=404,
                                        detail=f"EAP-4004 工作流 {name} 版本 {other_id} 不存在")
        to_dsl = other.dsl or {}
        to_meta = _version_meta(other)
    changes = workflow_versions.diff_dsl(row.dsl or {}, to_dsl)
    return {"workflow": name, "from": version_id, "to": other_id,
            "from_meta": _version_meta(row), "to_meta": to_meta, "changes": changes}


@router.post("/reload", dependencies=[fastapi.Depends(require_admin)])
async def reload_workflows(request: fastapi.Request):
    """从 DB 重新加载启用的 DSL 工作流（幂等注册）。"""
    count = await load_enabled()
    audit.record("workflow.reload", actor=audit.actor_of(request), target="*",
                 detail={"reloaded": count}, trace_id=getattr(request.state, "trace_id", ""))
    return {"reloaded": count}


@router.delete("/{name}", dependencies=[fastapi.Depends(require_admin)])
async def disable_workflow(name: str, request: fastapi.Request):
    try:
        await disable(name)
    except KeyError as e:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 {e}") from e
    audit.record("workflow.disable", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "enabled": False}

"""Workflow 版本化 + prod-env- 环境体系运行时（M32，v0.9 生产化）。

对齐 Agent 配置版本层（runtime/agent_config.py）的流水线语义：
- save_draft：从当前 dsl 存草稿（version = max+1 递增）
- publish(version_id, env)：DSL 可解析校验 → state=published、env 绑定、published_at；
  同 env 旧 published 自动 archived；env=prod 同步 WorkflowRecord.published_version_id 指针
- rollback(env)：该 env 最近 archived 重发布，现 published 归档（prod 同步指针）
- resolve_dsl 执行解析链：显式 version > env 指定 > prod 指针 > WorkflowRecord.dsl 兜底——
  无任何版本记录时行为与 M32 之前完全一致（存量工作流零影响，向后兼容）
"""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WorkflowRecord, WorkflowVersionRecord, utcnow
from .workflow import WorkflowSpec

# 受支持环境（M32 prod-env- 体系）：dev → test → staging → prod
ENVS: tuple[str, ...] = ("dev", "test", "staging", "prod")


def validate_env(env: str) -> str:
    """环境名白名单（发布/回滚/解析链共用）。"""
    if env not in ENVS:
        raise ValueError(f"EAP-4000 未知环境 {env!r}，允许: {list(ENVS)}")
    return env


def save_draft(db: Session, workflow: WorkflowRecord, dsl: dict, note: str = "") -> WorkflowVersionRecord:
    """存草稿：version = 该工作流 max+1（含历史草稿/发布，单调递增不复用）。"""
    if not isinstance(dsl, dict) or not dsl:
        raise ValueError("EAP-4000 dsl 必须是非空对象")
    last = db.scalar(select(WorkflowVersionRecord.version)
                     .where(WorkflowVersionRecord.workflow_id == workflow.id)
                     .order_by(WorkflowVersionRecord.version.desc()).limit(1))
    record = WorkflowVersionRecord(workflow_id=workflow.id, version=(last or 0) + 1,
                                   dsl=dsl, state="draft", note=note)
    db.add(record)
    db.flush()
    return record


def publish(db: Session, workflow: WorkflowRecord, version_id: int, env: str) -> WorkflowVersionRecord:
    """发布到环境：DSL 校验（复用 WorkflowSpec 解析）→ 绑定 env，同 env 旧版归档。

    - env=prod 时同步 WorkflowRecord.published_version_id（生产指针，workflow-as-agent 生效）；
      已发布版改投他 env（降级）时清空悬空的生产指针
    - 单一 env 绑定：一条版本记录同一时刻只属于一个 env，跨环境发布 = 迁移
    - 同一版本重复发布到同一 env 拒绝；DSL 校验复用 WorkflowSpec 解析（坏 DSL 挡在发布前）
    """
    validate_env(env)
    record = db.get(WorkflowVersionRecord, version_id)
    if record is None or record.workflow_id != workflow.id:
        raise KeyError(f"工作流 {workflow.name} 版本 {version_id} 不存在")
    if record.state == "published" and record.env == env:
        raise ValueError(f"EAP-4006 版本 {record.version} 已发布到 {env}")
    try:
        WorkflowSpec(**(record.dsl or {}))  # 发布期防线：坏 DSL 挡在发布前
    except ValidationError as e:
        raise ValueError(f"EAP-4000 版本 {record.version} DSL 不可解析: {e.errors()[0]['msg']}") from e
    except TypeError as e:
        raise ValueError(f"EAP-4000 版本 {record.version} DSL 不可解析: {e}") from e
    for row in db.scalars(select(WorkflowVersionRecord)
                          .where(WorkflowVersionRecord.workflow_id == workflow.id,
                                 WorkflowVersionRecord.state == "published",
                                 WorkflowVersionRecord.env == env)).all():
        if row.id != record.id:
            row.state = "archived"
    record.state = "published"
    # 单一 env 绑定：版本晋升到新环境即离开旧环境（一条记录同一时刻只属于一个 env）
    if env == "prod":
        workflow.published_version_id = record.id
    elif workflow.published_version_id == record.id:
        workflow.published_version_id = None  # prod 已发布版降级/改投他 env → 生产指针清空
    record.env = env
    record.published_at = utcnow()
    db.flush()
    return record


def rollback(db: Session, workflow: WorkflowRecord, env: str) -> WorkflowVersionRecord | None:
    """回滚该环境：现 published → archived，最近 archived（version 最大）→ published。

    该 env 无可回滚的归档版本时返回 None（端点层转 409），现状不变。
    """
    validate_env(env)
    current = db.scalar(select(WorkflowVersionRecord)
                        .where(WorkflowVersionRecord.workflow_id == workflow.id,
                               WorkflowVersionRecord.state == "published",
                               WorkflowVersionRecord.env == env))
    previous = db.scalar(select(WorkflowVersionRecord)
                         .where(WorkflowVersionRecord.workflow_id == workflow.id,
                                WorkflowVersionRecord.state == "archived",
                                WorkflowVersionRecord.env == env)
                         .order_by(WorkflowVersionRecord.version.desc()).limit(1))
    if previous is None:
        return None
    if current is not None:
        current.state = "archived"
        if env != "prod" and workflow.published_version_id == current.id:
            workflow.published_version_id = None  # 被归档版挂着生产指针 → 清空（env != prod）
    previous.state = "published"
    previous.published_at = utcnow()
    if env == "prod":
        workflow.published_version_id = previous.id
    db.flush()
    return previous


def diff_dsl(a: dict, b: dict) -> list[dict]:
    """两版 DSL 的结构化差异（仅报告不同项；形态对齐 agent diff 端点的 changes 列表）。

    - 顶层标量（name/version/description）逐键；steps 按节点 id 对齐（增/删/逐字段）；
      edges 按边 id 对齐（增/删/逐字段）
    - key 命名：steps.<id>[.<field>] / edges.<id>[.<field>]，from/to 为 None 表示新增/删除
    """
    changes: list[dict] = []
    for key in ("name", "version", "description"):
        if (a.get(key) or "") != (b.get(key) or ""):
            changes.append({"key": key, "from": a.get(key), "to": b.get(key)})
    steps_a = {s.get("id"): s for s in (a.get("steps") or [])}
    steps_b = {s.get("id"): s for s in (b.get("steps") or [])}
    for sid in sorted(set(steps_a) | set(steps_b)):
        sa, sb = steps_a.get(sid), steps_b.get(sid)
        if sa is None or sb is None:
            changes.append({"key": f"steps.{sid}", "from": sa, "to": sb})
            continue
        for field in sorted(set(sa) | set(sb)):
            if sa.get(field) != sb.get(field):
                changes.append({"key": f"steps.{sid}.{field}", "from": sa.get(field),
                                "to": sb.get(field)})
    edges_a = {e.get("id"): e for e in (a.get("edges") or [])}
    edges_b = {e.get("id"): e for e in (b.get("edges") or [])}
    for eid in sorted(set(edges_a) | set(edges_b)):
        ea, eb = edges_a.get(eid), edges_b.get(eid)
        if ea is None or eb is None:
            changes.append({"key": f"edges.{eid}", "from": ea, "to": eb})
            continue
        for field in sorted(set(ea) | set(eb)):
            if ea.get(field) != eb.get(field):
                changes.append({"key": f"edges.{eid}.{field}", "from": ea.get(field),
                                "to": eb.get(field)})
    return changes


def resolve_dsl(db: Session, workflow: WorkflowRecord,
                version: int | None = None, env: str | None = None) -> tuple[dict, int | None]:
    """执行解析链（M32）：显式 version > env 指定 > prod 指针 > WorkflowRecord.dsl 兜底。

    - version：按 (workflow_id, version) 精确取（任意状态——test-run 预览草稿用）
    - env：该 env state=published 的版本；该 env 未发布过则落到下一级
    - 指针：WorkflowRecord.published_version_id（仅 env=prod 发布时写入）
    - 兜底：WorkflowRecord.dsl（存量工作流零影响——没有版本记录时行为不变）
    返回 (dsl, 命中的版本记录 id | None)。
    """
    if version is not None:
        record = db.scalar(select(WorkflowVersionRecord)
                           .where(WorkflowVersionRecord.workflow_id == workflow.id,
                                  WorkflowVersionRecord.version == version))
        if record is None:
            raise KeyError(f"工作流 {workflow.name} 版本 {version} 不存在")
        return record.dsl or {}, record.id
    if env is not None:
        validate_env(env)
        record = db.scalar(select(WorkflowVersionRecord)
                           .where(WorkflowVersionRecord.workflow_id == workflow.id,
                                  WorkflowVersionRecord.state == "published",
                                  WorkflowVersionRecord.env == env)
                           .order_by(WorkflowVersionRecord.version.desc()).limit(1))
        if record is not None:
            return record.dsl or {}, record.id
    if workflow.published_version_id:
        record = db.get(WorkflowVersionRecord, workflow.published_version_id)
        if record is not None and record.state == "published" and record.env == "prod":
            return record.dsl or {}, record.id
    return workflow.dsl or {}, None

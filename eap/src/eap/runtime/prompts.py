"""Prompt 版本流水线与 A/B 实验（docs/05 §3，M3）。

- 版本：draft → publish（写入 PromptRecord 发布指针，旧版 archived）→ rollback（重发布上一个 archived）
- A/B：实验绑定 prompt 的两个版本，按 key 稳定 hash（SHA-256）分流；
  同一 key 永远命中同一版本，percent_b=0/100 分别为全 A / 全 B。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PromptExperimentRecord, PromptRecord, PromptVersionRecord


def resolve_template(db: Session, name: str, key: str | None = None) -> tuple[str, str, dict | None]:
    """返回 (template, version, experiment)。key 提供且命中实验 B 桶时返回 B 版本模板。

    版本缺失（实验指向未发布/已删除版本）时回落当前发布版，experiment 仍返回并标注 fallback。
    """
    record = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    if record is None:
        raise KeyError(f"Prompt {name} 不存在")
    if not key:
        return record.template, record.version, None

    experiment = db.scalar(select(PromptExperimentRecord)
                           .where(PromptExperimentRecord.prompt_name == name,
                                  PromptExperimentRecord.enabled == True)  # noqa: E712
                           .order_by(PromptExperimentRecord.id.desc()))  # 最新实验优先
    if experiment is None or experiment.percent_b <= 0:
        return record.template, record.version, None

    digest = hashlib.sha256(f"prompt:{name}:{key}".encode()).hexdigest()
    hit_b = int(digest[:8], 16) % 100 < experiment.percent_b
    if not hit_b:
        return record.template, record.version, _exp_view(experiment, picked="a")

    version_b = db.scalar(select(PromptVersionRecord)
                          .where(PromptVersionRecord.name == name,
                                 PromptVersionRecord.version == experiment.version_b))
    if version_b is None:
        return record.template, record.version, _exp_view(experiment, picked="fallback")
    return version_b.template, version_b.version, _exp_view(experiment, picked="b")


def _exp_view(e: PromptExperimentRecord, picked: str) -> dict:
    return {"experiment": e.name, "version_a": e.version_a, "version_b": e.version_b,
            "percent_b": e.percent_b, "picked": picked}


# ---------- 版本流水线 ----------

def create_version(db: Session, name: str, version: str, template: str, notes: str) -> PromptVersionRecord:
    exists = db.scalar(select(PromptVersionRecord)
                       .where(PromptVersionRecord.name == name, PromptVersionRecord.version == version))
    if exists:
        raise ValueError(f"EAP-2002 Prompt {name}@{version} 已存在")
    from .context import extract_prompt_variables

    record = PromptVersionRecord(name=name, version=version, template=template,
                                 variables=extract_prompt_variables(template),
                                 notes=notes, state="draft")
    db.add(record)
    db.flush()
    return record


def publish_version(db: Session, name: str, version: str, variables_sample: dict) -> PromptVersionRecord:
    """发布：先按样例变量试渲染（缺变量即拒绝），写入发布指针，旧发布版归档。"""
    target = db.scalar(select(PromptVersionRecord)
                       .where(PromptVersionRecord.name == name, PromptVersionRecord.version == version))
    if target is None:
        raise KeyError(f"Prompt {name}@{version} 不存在")
    from .context import render_prompt

    try:
        render_prompt(target.template, variables_sample)
    except ValueError as e:
        raise ValueError(f"EAP-4000 发布校验失败：{e}") from e

    current = db.scalar(select(PromptRecord).where(PromptRecord.name == name))
    previous_version = current.version if current else None
    if current is None:
        current = PromptRecord(name=name)
        db.add(current)
    current.version = target.version
    current.template = target.template
    current.variables = target.variables
    current.enabled = True

    # 归档旧的 published（不含本次目标）
    for row in db.scalars(select(PromptVersionRecord)
                          .where(PromptVersionRecord.name == name,
                                 PromptVersionRecord.state == "published")).all():
        if row.version != version:
            row.state = "archived"
    target.state = "published"
    target.notes = target.notes or f"previous={previous_version}"
    db.flush()
    return target


def rollback_version(db: Session, name: str) -> PromptVersionRecord | None:
    """回滚：把最近一个 archived 版本重新发布（用其自带变量清单构造空值样例过校验）。"""
    previous = db.scalars(
        select(PromptVersionRecord)
        .where(PromptVersionRecord.name == name, PromptVersionRecord.state == "archived")
        .order_by(PromptVersionRecord.created_at.desc())).first()
    if previous is None:
        return None
    sample = {v: "" for v in (previous.variables or [])}
    return publish_version(db, name, previous.version, sample)

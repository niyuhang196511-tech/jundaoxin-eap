"""工作流服务：DSL 存储 → 动态注册为智能体 → 平台纳管（docs/03 §6）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agents.registry import registry
from .db import SessionLocal
from .models import WorkflowRecord
from .runtime.workflow import WorkflowSpec, create_workflow_agent_class


async def create_and_register(db: Session, spec: WorkflowSpec) -> WorkflowRecord:
    """持久化 DSL → 动态构建 AgentApp → 注册 → 启动（对平台即普通智能体）。"""
    if db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == spec.name)):
        raise ValueError(f"工作流 {spec.name} 已存在")
    record = WorkflowRecord(name=spec.name, version=spec.version, dsl=spec.model_dump())
    db.add(record)
    db.commit()

    cls = create_workflow_agent_class(spec)
    registry.register(cls, cls.manifest, source="workflow", module=f"workflow:{spec.name}")
    await registry.start_agent(spec.name)
    return record


async def load_enabled() -> int:
    """平台启动时：把已启用的 DSL 工作流重新注册为智能体。"""
    count = 0
    with SessionLocal() as db:
        for record in db.scalars(select(WorkflowRecord).where(WorkflowRecord.enabled == True)).all():  # noqa: E712
            try:
                spec = WorkflowSpec(**record.dsl)
                cls = create_workflow_agent_class(spec)
                registry.register(cls, cls.manifest, source="workflow",
                                  module=f"workflow:{spec.name}")
                await registry.start_agent(spec.name)
                count += 1
            except Exception as e:
                print(f"[workflow] {record.name} 注册失败: {e}")
    return count


async def disable(name: str) -> None:
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        if record is None:
            raise KeyError(f"工作流 {name} 不存在")
        record.enabled = False
        db.commit()
    registry._agents.pop(name, None)  # 从目录摘除（版本历史仍在 DB）

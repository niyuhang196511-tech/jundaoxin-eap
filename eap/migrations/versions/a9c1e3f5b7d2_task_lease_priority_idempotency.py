"""tasks: worker lease + queue priority + idempotency key

Revision ID: a9c1e3f5b7d2
Revises: d7a1b3c5e9f2
Create Date: 2026-09-21

M32 任务组 P1：prod-worker- 分布式 Worker / 队列深化——
- lease_expires_at：执行租约到期时刻（worker 取任务时置 now+租约，终态清空）；
  周期扫描重置「RUNNING 且租约过期」的任务（worker 崩溃兜底）
- priority：队列优先级（int，数值大优先、同级 FIFO），存量行 server_default 0
- idempotency_key：任务幂等键（nullable，加索引；同 key 在途任务命中直接返回）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a9c1e3f5b7d2'
down_revision: Union[str, Sequence[str], None] = 'd7a1b3c5e9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 待补列清单：列名 → (SQLAlchemy 类型, server_default)
_NEW_COLUMNS: dict[str, tuple[sa.types.TypeEngine, str | None]] = {
    "lease_expires_at": (sa.DateTime(), None),
    "priority": (sa.Integer(), "0"),
    "idempotency_key": (sa.String(length=128), None),
}

_INDEX_NAME = "ix_tasks_idempotency_key"


def _existing_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("tasks")}


def _existing_indexes(conn) -> set[str]:
    return {i["name"] for i in sa.inspect(conn).get_indexes("tasks")}


def upgrade() -> None:
    """tasks 表补列 + 幂等键索引。幂等（列/索引已存在则跳过，baseline create_all 已含）。"""
    conn = op.get_bind()
    existing = _existing_columns(conn)
    for name, (col_type, server_default) in _NEW_COLUMNS.items():
        if name not in existing:
            op.add_column("tasks",
                          sa.Column(name, col_type, nullable=True, server_default=server_default))
    if _INDEX_NAME not in _existing_indexes(conn):
        op.create_index(_INDEX_NAME, "tasks", ["idempotency_key"])


def downgrade() -> None:
    """Downgrade：回退本迁移补的索引与列（仅删确实存在的）。"""
    conn = op.get_bind()
    if _INDEX_NAME in _existing_indexes(conn):
        op.drop_index(_INDEX_NAME, table_name="tasks")
    existing = _existing_columns(conn)
    for name in _NEW_COLUMNS:
        if name in existing:
            op.drop_column("tasks", name)

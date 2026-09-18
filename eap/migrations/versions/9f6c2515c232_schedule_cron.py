"""schedule cron

Revision ID: 9f6c2515c232
Revises: 4d2bbe9ab7fa
Create Date: 2026-09-16 21:10:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9f6c2515c232'
down_revision: Union[str, Sequence[str], None] = '4d2bbe9ab7fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """定时调度 cron 表达式列（M17）。存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    cols = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(task_schedules)")).fetchall()] \
        if conn.dialect.name == "sqlite" else \
        [c["name"] for c in sa.inspect(conn).get_columns("task_schedules")]
    if "cron" not in cols:
        op.add_column('task_schedules', sa.Column('cron', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade。"""
    op.drop_column('task_schedules', 'cron')

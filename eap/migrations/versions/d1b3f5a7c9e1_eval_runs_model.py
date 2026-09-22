"""eval runs model direct evaluation

Revision ID: d1b3f5a7c9e1
Revises: c9f1e3a5d7b9
Create Date: 2026-09-22 10:20:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1b3f5a7c9e1'
down_revision: Union[str, Sequence[str], None] = 'c9f1e3a5d7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    """模型直评（M42-B）：eval_runs 加 model 列（评测门禁接入模型路由的数据基础）。幂等。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "eval_runs" in tables:
        cols = _columns(conn, "eval_runs")
        if "model" not in cols:
            op.add_column('eval_runs', sa.Column('model', sa.String(length=128),
                                                 nullable=True))
            op.create_index('ix_eval_runs_model', 'eval_runs', ['model'])


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "eval_runs" in tables:
        cols = _columns(conn, "eval_runs")
        if "model" in cols:
            op.drop_index('ix_eval_runs_model', table_name='eval_runs')
            op.drop_column('eval_runs', 'model')

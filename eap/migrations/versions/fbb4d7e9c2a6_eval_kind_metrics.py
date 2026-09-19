"""eval datasets kind + runs metrics

Revision ID: fbb4d7e9c2a6
Revises: fa1b5c8d9e2f
Create Date: 2026-09-19 10:20:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fbb4d7e9c2a6'
down_revision: Union[str, Sequence[str], None] = 'fa1b5c8d9e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    """评测深化列（v0.6-③④）：数据集 kind；运行 kind/pass_rate/metrics。幂等。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "eval_datasets" in tables and "kind" not in _columns(conn, "eval_datasets"):
        op.add_column('eval_datasets', sa.Column('kind', sa.String(length=8),
                                                 nullable=False, server_default='agent'))
    if "eval_runs" in tables:
        cols = _columns(conn, "eval_runs")
        if "kind" not in cols:
            op.add_column('eval_runs', sa.Column('kind', sa.String(length=8),
                                                 nullable=False, server_default='agent'))
        if "pass_rate" not in cols:
            op.add_column('eval_runs', sa.Column('pass_rate', sa.Float(), nullable=True))
        if "metrics" not in cols:
            op.add_column('eval_runs', sa.Column('metrics', sa.JSON(), nullable=False,
                                                 server_default='{}'))


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "eval_runs" in tables:
        cols = _columns(conn, "eval_runs")
        if "metrics" in cols:
            op.drop_column('eval_runs', 'metrics')
        if "pass_rate" in cols:
            op.drop_column('eval_runs', 'pass_rate')
        if "kind" in cols:
            op.drop_column('eval_runs', 'kind')
    if "eval_datasets" in tables and "kind" in _columns(conn, "eval_datasets"):
        op.drop_column('eval_datasets', 'kind')

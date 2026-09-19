"""workflow runs suspend/resume columns

Revision ID: e9f3b6d2a7c5
Revises: d7e2a4b8c1f3
Create Date: 2026-09-18 17:40:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9f3b6d2a7c5'
down_revision: Union[str, Sequence[str], None] = 'd7e2a4b8c1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _run_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("workflow_runs")}


def upgrade() -> None:
    """interaction 节点挂起/恢复列（v0.5-⑤）。已存在则幂等跳过。"""
    conn = op.get_bind()
    if "workflow_runs" not in sa.inspect(conn).get_table_names():
        return
    cols = _run_columns(conn)
    if "variables" not in cols:
        op.add_column('workflow_runs', sa.Column('variables', sa.JSON(), nullable=False,
                                                 server_default='{}'))
    if "pending_node" not in cols:
        op.add_column('workflow_runs', sa.Column('pending_node', sa.String(length=64),
                                                 nullable=True))


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "workflow_runs" in sa.inspect(conn).get_table_names():
        cols = _run_columns(conn)
        if "pending_node" in cols:
            op.drop_column('workflow_runs', 'pending_node')
        if "variables" in cols:
            op.drop_column('workflow_runs', 'variables')

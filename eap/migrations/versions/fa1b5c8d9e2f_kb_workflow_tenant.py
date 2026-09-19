"""kb workflow tenant columns

Revision ID: fa1b5c8d9e2f
Revises: e9f3b6d2a7c5
Create Date: 2026-09-19 09:30:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fa1b5c8d9e2f'
down_revision: Union[str, Sequence[str], None] = 'e9f3b6d2a7c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    """kbs/workflows 补 tenant_id（v0.6 与 rls.py 清单对齐）。已存在则幂等跳过。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "kbs" in tables and "tenant_id" not in _columns(conn, "kbs"):
        op.add_column('kbs', sa.Column('tenant_id', sa.Integer(), nullable=True, index=True))
    if "workflows" in tables and "tenant_id" not in _columns(conn, "workflows"):
        op.add_column('workflows', sa.Column('tenant_id', sa.Integer(), nullable=True, index=True))


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "workflows" in tables and "tenant_id" in _columns(conn, "workflows"):
        op.drop_column('workflows', 'tenant_id')
    if "kbs" in tables and "tenant_id" in _columns(conn, "kbs"):
        op.drop_column('kbs', 'tenant_id')

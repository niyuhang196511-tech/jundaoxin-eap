"""cost metering columns

Revision ID: a2c4e6f8b1d3
Revises: fbb4d7e9c2a6
Create Date: 2026-09-19 11:05:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a2c4e6f8b1d3'
down_revision: Union[str, Sequence[str], None] = 'fbb4d7e9c2a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    """成本计量列（v0.6-⑤）：模型定价 + 用量 agent/cost 维度。幂等。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "models" in tables:
        cols = _columns(conn, "models")
        if "price_in" not in cols:
            op.add_column('models', sa.Column('price_in', sa.Float(), nullable=True))
        if "price_out" not in cols:
            op.add_column('models', sa.Column('price_out', sa.Float(), nullable=True))
    if "usage_records" in tables:
        cols = _columns(conn, "usage_records")
        if "agent" not in cols:
            op.add_column('usage_records', sa.Column('agent', sa.String(length=64),
                                                     nullable=False, server_default='', index=True))
        if "cost" not in cols:
            op.add_column('usage_records', sa.Column('cost', sa.Float(),
                                                     nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "usage_records" in tables:
        cols = _columns(conn, "usage_records")
        if "cost" in cols:
            op.drop_column('usage_records', 'cost')
        if "agent" in cols:
            op.drop_column('usage_records', 'agent')
    if "models" in tables:
        cols = _columns(conn, "models")
        if "price_out" in cols:
            op.drop_column('models', 'price_out')
        if "price_in" in cols:
            op.drop_column('models', 'price_in')

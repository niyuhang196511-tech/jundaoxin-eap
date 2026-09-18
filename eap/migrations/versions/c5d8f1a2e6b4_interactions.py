"""interactions table

Revision ID: c5d8f1a2e6b4
Revises: b3e7f2a9c4d1
Create Date: 2026-09-18 16:05:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c5d8f1a2e6b4'
down_revision: Union[str, Sequence[str], None] = 'b3e7f2a9c4d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """交互引擎 interactions 表（v0.5-④）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    if "interactions" not in sa.inspect(conn).get_table_names():
        op.create_table(
            'interactions',
            sa.Column('id', sa.String(length=40), primary_key=True),
            sa.Column('agent', sa.String(length=64), nullable=False, index=True),
            sa.Column('session_id', sa.String(length=64), nullable=True, index=True),
            sa.Column('task_id', sa.String(length=64), nullable=True, index=True),
            sa.Column('run_id', sa.String(length=40), nullable=True, index=True),
            sa.Column('schema', sa.JSON(), nullable=False),
            sa.Column('values', sa.JSON(), nullable=False),
            sa.Column('state', sa.String(length=16), nullable=False, server_default='waiting', index=True),
            sa.Column('trace_id', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "interactions" in sa.inspect(conn).get_table_names():
        op.drop_table('interactions')

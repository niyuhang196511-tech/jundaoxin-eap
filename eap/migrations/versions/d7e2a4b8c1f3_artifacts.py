"""artifacts table

Revision ID: d7e2a4b8c1f3
Revises: c5d8f1a2e6b4
Create Date: 2026-09-18 17:10:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7e2a4b8c1f3'
down_revision: Union[str, Sequence[str], None] = 'c5d8f1a2e6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Artifact / File Center（v0.5-⑦）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    if "artifacts" not in sa.inspect(conn).get_table_names():
        op.create_table(
            'artifacts',
            sa.Column('id', sa.String(length=40), primary_key=True),
            sa.Column('name', sa.String(length=128), nullable=False, server_default=''),
            sa.Column('type', sa.String(length=16), nullable=False, server_default='md'),
            sa.Column('mime', sa.String(length=64), nullable=False, server_default='text/markdown'),
            sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('sha256', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('agent', sa.String(length=64), nullable=False, server_default='', index=True),
            sa.Column('session_id', sa.String(length=64), nullable=True, index=True),
            sa.Column('task_id', sa.String(length=64), nullable=True, index=True),
            sa.Column('storage_path', sa.String(length=256), nullable=False, server_default=''),
            sa.Column('expires_at', sa.DateTime(), nullable=True, index=True),
            sa.Column('trace_id', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False, index=True),
        )


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "artifacts" in sa.inspect(conn).get_table_names():
        op.drop_table('artifacts')

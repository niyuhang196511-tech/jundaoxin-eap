"""extension records

Revision ID: bd3e7f2a4c19
Revises: a2c4e6f8b1d3
Create Date: 2026-09-19 12:40:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'bd3e7f2a4c19'
down_revision: Union[str, Sequence[str], None] = 'a2c4e6f8b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """扩展注册表（v0.7-⑨）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    if "extension_records" not in sa.inspect(conn).get_table_names():
        op.create_table(
            'extension_records',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=64), nullable=False, unique=True, index=True),
            sa.Column('type', sa.String(length=20), nullable=False, index=True),
            sa.Column('version', sa.String(length=32), nullable=False, server_default='0.0.0'),
            sa.Column('title', sa.String(length=128), nullable=False, server_default=''),
            sa.Column('description', sa.String(length=256), nullable=False, server_default=''),
            sa.Column('manifest', sa.JSON(), nullable=False),
            sa.Column('source', sa.String(length=16), nullable=False, server_default='plugin_dir'),
            sa.Column('module', sa.String(length=200), nullable=False, server_default=''),
            sa.Column('state', sa.String(length=16), nullable=False, server_default='registered', index=True),
            sa.Column('exposes', sa.JSON(), nullable=False),
            sa.Column('error', sa.String(length=512), nullable=False, server_default=''),
            sa.Column('installed_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "extension_records" in sa.inspect(conn).get_table_names():
        op.drop_table('extension_records')

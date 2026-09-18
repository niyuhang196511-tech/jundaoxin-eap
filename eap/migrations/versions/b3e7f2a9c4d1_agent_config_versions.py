"""agent config versions

Revision ID: b3e7f2a9c4d1
Revises: 9f6c2515c232
Create Date: 2026-09-18 11:20:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3e7f2a9c4d1'
down_revision: Union[str, Sequence[str], None] = '9f6c2515c232'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _agent_columns(conn) -> set[str]:
    """agents 表现有列名（SQLAlchemy inspector，SQLite/PG 通用）。"""
    return {c["name"] for c in sa.inspect(conn).get_columns("agents")}


def upgrade() -> None:
    """Agent 配置版本层（v0.5-①）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "agent_versions" not in tables:
        op.create_table(
            'agent_versions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('agent_name', sa.String(length=64), nullable=False, index=True),
            sa.Column('version', sa.String(length=32), nullable=False),
            sa.Column('config', sa.JSON(), nullable=False),
            sa.Column('state', sa.String(length=16), nullable=False, server_default='draft', index=True),
            sa.Column('notes', sa.String(length=256), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('agent_name', 'version', name='uq_agentver_name_version'),
        )
    if "agents" in tables and "published_version" not in _agent_columns(conn):
        op.add_column('agents', sa.Column('published_version', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "agents" in tables and "published_version" in _agent_columns(conn):
        op.drop_column('agents', 'published_version')
    if "agent_versions" in tables:
        op.drop_table('agent_versions')

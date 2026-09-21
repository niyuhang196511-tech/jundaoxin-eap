"""workflow versioning + prod envs

Revision ID: b8d2f4a6c0e3
Revises: a9c1e3f5b7d2
Create Date: 2026-09-21 10:00:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8d2f4a6c0e3'
down_revision: Union[str, Sequence[str], None] = 'a9c1e3f5b7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Workflow 版本化 + prod-env- 环境体系（M32）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "workflow_versions" not in tables:
        op.create_table(
            'workflow_versions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('workflow_id', sa.Integer(),
                      sa.ForeignKey('workflows.id'), nullable=False, index=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('dsl', sa.JSON(), nullable=False),
            sa.Column('env', sa.String(length=16), nullable=True, index=True),
            sa.Column('state', sa.String(length=16), nullable=False,
                      server_default='draft', index=True),
            sa.Column('note', sa.String(length=256), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('published_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('workflow_id', 'version', name='uq_wfver_wf_version'),
        )
    if "workflows" in tables:
        wf_columns = {c["name"] for c in sa.inspect(conn).get_columns("workflows")}
        if "published_version_id" not in wf_columns:
            op.add_column('workflows', sa.Column('published_version_id',
                                                 sa.Integer(), nullable=True, index=True))


def downgrade() -> None:
    """Downgrade。SQLite 上 DROP COLUMN 前须先删该列索引。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "workflows" in tables:
        wf_columns = {c["name"] for c in sa.inspect(conn).get_columns("workflows")}
        if "published_version_id" in wf_columns:
            indexes = {ix["name"] for ix in sa.inspect(conn).get_indexes("workflows")}
            if "ix_workflows_published_version_id" in indexes:
                op.drop_index('ix_workflows_published_version_id', table_name='workflows')
            op.drop_column('workflows', 'published_version_id')
    if "workflow_versions" in tables:
        op.drop_table('workflow_versions')

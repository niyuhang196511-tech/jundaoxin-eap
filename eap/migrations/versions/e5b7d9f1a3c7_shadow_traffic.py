"""shadow traffic online evaluation

Revision ID: e5b7d9f1a3c7
Revises: d1b3f5a7c9e1
Create Date: 2026-09-23 12:00:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5b7d9f1a3c7'
down_revision: Union[str, Sequence[str], None] = 'd1b3f5a7c9e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def upgrade() -> None:
    """影子流量（M44-A）：shadow_configs + shadow_runs 两表。幂等。"""
    conn = op.get_bind()
    tables = _tables(conn)
    if "shadow_configs" not in tables:
        op.create_table(
            'shadow_configs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=64), nullable=False),
            sa.Column('source_agent', sa.String(length=64), nullable=False),
            sa.Column('shadow_agent', sa.String(length=64), nullable=False),
            sa.Column('sample_rate', sa.Float(), nullable=True),
            sa.Column('judge_criteria', sa.Text(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=True),
            sa.Column('note', sa.String(length=512), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_shadow_configs_name', 'shadow_configs', ['name'], unique=True)
        op.create_index('ix_shadow_configs_source_agent', 'shadow_configs', ['source_agent'])
        op.create_index('ix_shadow_configs_shadow_agent', 'shadow_configs', ['shadow_agent'])
    if "shadow_runs" not in tables:
        op.create_table(
            'shadow_runs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('config_id', sa.Integer(), nullable=False),
            sa.Column('config_name', sa.String(length=64), nullable=False),
            sa.Column('trace_id', sa.String(length=64), nullable=False),
            sa.Column('source_agent', sa.String(length=64), nullable=True),
            sa.Column('shadow_agent', sa.String(length=64), nullable=True),
            sa.Column('input_text', sa.Text(), nullable=True),
            sa.Column('primary_output', sa.Text(), nullable=True),
            sa.Column('primary_latency_ms', sa.Integer(), nullable=True),
            sa.Column('shadow_output', sa.Text(), nullable=True),
            sa.Column('shadow_latency_ms', sa.Integer(), nullable=True),
            sa.Column('shadow_error', sa.String(length=512), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_shadow_runs_config_id', 'shadow_runs', ['config_id'])
        op.create_index('ix_shadow_runs_config_name', 'shadow_runs', ['config_name'])
        op.create_index('ix_shadow_runs_trace_id', 'shadow_runs', ['trace_id'])


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    tables = _tables(conn)
    if "shadow_runs" in tables:
        op.drop_table('shadow_runs')
    if "shadow_configs" in tables:
        op.drop_table('shadow_configs')

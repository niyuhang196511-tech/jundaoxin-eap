"""trigger rules（M30 事件中心：event/cron/webhook → agent/workflow 触发规则）

Revision ID: a3e5f7b9c1d2
Revises: bd3e7f2a4c19
Create Date: 2026-09-21 10:20:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3e5f7b9c1d2'
down_revision: Union[str, Sequence[str], None] = 'bd3e7f2a4c19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """触发规则表。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    if "trigger_rules" not in sa.inspect(conn).get_table_names():
        op.create_table(
            'trigger_rules',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=64), nullable=False, unique=True, index=True),
            sa.Column('tenant_id', sa.Integer(), nullable=True, index=True),
            sa.Column('source', sa.String(length=16), nullable=False, server_default='event'),
            sa.Column('event_type', sa.String(length=128), nullable=True),
            sa.Column('match', sa.JSON(), nullable=True),
            sa.Column('cron', sa.String(length=64), nullable=True),
            sa.Column('secret', sa.String(length=512), nullable=True),
            sa.Column('target_type', sa.String(length=16), nullable=False, server_default='agent'),
            sa.Column('target_name', sa.String(length=64), nullable=False),
            sa.Column('input_mode', sa.String(length=16), nullable=False, server_default='payload'),
            sa.Column('template', sa.JSON(), nullable=True),
            sa.Column('min_interval_s', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "trigger_rules" in sa.inspect(conn).get_table_names():
        op.drop_table('trigger_rules')

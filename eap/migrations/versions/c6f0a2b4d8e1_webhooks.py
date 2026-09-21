"""webhooks: outbound endpoints + delivery retry queue

Revision ID: c6f0a2b4d8e1
Revises: b4c6d8e0f2a4
Create Date: 2026-09-21

M31 任务组 B：对外 Webhook 推送——端点表 webhook_endpoints（URL/secret Fernet/订阅
pattern 列表）+ 投递记录/重试队列表 webhook_deliveries（失败指数退避重投，超限 dead）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c6f0a2b4d8e1'
down_revision: Union[str, Sequence[str], None] = 'b4c6d8e0f2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Webhook 端点表 + 投递记录表。幂等（表已存在则跳过，baseline create_all 已含）。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "webhook_endpoints" not in tables:
        op.create_table(
            'webhook_endpoints',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=64), nullable=False, unique=True, index=True),
            sa.Column('url', sa.String(length=512), nullable=False),
            sa.Column('secret', sa.String(length=512), nullable=True),
            sa.Column('events', sa.Text(), nullable=False, server_default='[]'),
            sa.Column('tenant_id', sa.Integer(), nullable=True, index=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
    if "webhook_deliveries" not in tables:
        op.create_table(
            'webhook_deliveries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('endpoint_id', sa.Integer(),
                      sa.ForeignKey('webhook_endpoints.id'), nullable=False),
            sa.Column('event_id', sa.String(length=64), nullable=False),
            sa.Column('event_type', sa.String(length=128), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=False, server_default='{}'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_retry_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=8), nullable=False, server_default='pending'),
            sa.Column('response_status', sa.Integer(), nullable=True),
            sa.Column('error', sa.String(length=512), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_webhook_deliveries_endpoint_id', 'webhook_deliveries', ['endpoint_id'])
        op.create_index('ix_webhook_deliveries_event_id', 'webhook_deliveries', ['event_id'])
        op.create_index('ix_webhook_deliveries_event_type', 'webhook_deliveries', ['event_type'])
        op.create_index('ix_webhook_deliveries_status', 'webhook_deliveries', ['status'])


def downgrade() -> None:
    """Downgrade：删投递记录表 + 端点表。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "webhook_deliveries" in tables:
        op.drop_table('webhook_deliveries')
    if "webhook_endpoints" in tables:
        op.drop_table('webhook_endpoints')

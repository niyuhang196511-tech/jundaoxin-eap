"""im outbound: app credentials + delivery retry queue

Revision ID: b4c6d8e0f2a4
Revises: a3e5f7b9c1d2
Create Date: 2026-09-21

M30 任务组 D：IM 渠道应用级凭据列（app_id/app_secret_enc，M26 Fernet 同款加密）
+ 投递日志/重试队列表 im_outbound_logs（幂等键 channel+event_key 唯一约束）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b4c6d8e0f2a4'
down_revision: Union[str, Sequence[str], None] = 'a3e5f7b9c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    """IM 应用凭据列 + 投递重试队列表。幂等（列/表存在则跳过）。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "im_channels" in tables:
        cols = _columns(conn, "im_channels")
        if "app_id" not in cols:
            op.add_column('im_channels', sa.Column('app_id', sa.String(length=128),
                                                   nullable=False, server_default=''))
        if "app_secret_enc" not in cols:
            op.add_column('im_channels', sa.Column('app_secret_enc', sa.String(length=256),
                                                   nullable=True))
    if "im_outbound_logs" not in tables:
        op.create_table(
            'im_outbound_logs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('channel_id', sa.Integer(), nullable=False),
            sa.Column('direction', sa.String(length=4), nullable=False, server_default='out'),
            sa.Column('event_key', sa.String(length=128), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=False, server_default='{}'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_retry_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=8), nullable=False, server_default='pending'),
            sa.Column('error', sa.String(length=512), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('channel_id', 'event_key', name='uq_im_outbound_channel_event'),
        )
        op.create_index('ix_im_outbound_logs_channel_id', 'im_outbound_logs', ['channel_id'])
        op.create_index('ix_im_outbound_logs_event_key', 'im_outbound_logs', ['event_key'])
        op.create_index('ix_im_outbound_logs_status', 'im_outbound_logs', ['status'])


def downgrade() -> None:
    """Downgrade：删队列表 + 凭据列。"""
    conn = op.get_bind()
    tables = sa.inspect(conn).get_table_names()
    if "im_outbound_logs" in tables:
        op.drop_table('im_outbound_logs')
    if "im_channels" in tables:
        cols = _columns(conn, "im_channels")
        if "app_secret_enc" in cols:
            op.drop_column('im_channels', 'app_secret_enc')
        if "app_id" in cols:
            op.drop_column('im_channels', 'app_id')

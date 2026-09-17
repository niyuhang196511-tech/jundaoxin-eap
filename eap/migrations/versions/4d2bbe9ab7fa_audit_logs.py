"""audit logs

Revision ID: 4d2bbe9ab7fa
Revises: 96c40058923b
Create Date: 2026-09-16 20:05:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4d2bbe9ab7fa'
down_revision: Union[str, Sequence[str], None] = '96c40058923b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """审计日志表（M11）。baseline（create_all）已含本表时幂等跳过。"""
    from sqlalchemy import inspect as _inspect

    if 'audit_logs' in _inspect(op.get_bind()).get_table_names():
        return
    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=128), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    op.create_index(op.f('ix_audit_logs_created_at'), 'audit_logs', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade。"""
    op.drop_index(op.f('ix_audit_logs_created_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_action'), table_name='audit_logs')
    op.drop_table('audit_logs')

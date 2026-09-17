"""revoked tokens

Revision ID: 96c40058923b
Revises: a1f2c3d4e5f6
Create Date: 2026-09-16 19:32:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '96c40058923b'
down_revision: Union[str, Sequence[str], None] = 'a1f2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """JWT 吊销黑名单表（M10）。baseline（create_all）已含本表时幂等跳过。"""
    from sqlalchemy import inspect as _inspect

    if 'revoked_tokens' in _inspect(op.get_bind()).get_table_names():
        return
    op.create_table('revoked_tokens',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=False),
    sa.Column('reason', sa.String(length=128), nullable=False),
    sa.PrimaryKeyConstraint('token_hash')
    )
    op.create_index(op.f('ix_revoked_tokens_expires_at'), 'revoked_tokens', ['expires_at'], unique=False)


def downgrade() -> None:
    """Downgrade。"""
    op.drop_index(op.f('ix_revoked_tokens_expires_at'), table_name='revoked_tokens')
    op.drop_table('revoked_tokens')

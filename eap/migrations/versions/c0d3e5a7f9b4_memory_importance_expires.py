"""memory importance + expires_at（M34/L7：记忆深化）

Revision ID: c0d3e5a7f9b4
Revises: b8d2f4a6c0e3
Create Date: 2026-09-21 11:00:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c0d3e5a7f9b4'
down_revision: Union[str, Sequence[str], None] = 'b8d2f4a6c0e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """memories 加列：importance（float，默认 0.5）与 expires_at（TTL，可空）。

    幂等：列已存在则跳过（baseline create_all 已含新列的全新库直接过）。
    """
    conn = op.get_bind()
    columns = {c["name"] for c in sa.inspect(conn).get_columns("memories")}
    if "importance" not in columns:
        op.add_column('memories', sa.Column('importance', sa.Float(),
                                            nullable=False, server_default='0.5'))
    if "expires_at" not in columns:
        op.add_column('memories', sa.Column('expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """回退：删除新列（幂等，列不存在则跳过）。"""
    conn = op.get_bind()
    columns = {c["name"] for c in sa.inspect(conn).get_columns("memories")}
    if "expires_at" in columns:
        op.drop_column('memories', 'expires_at')
    if "importance" in columns:
        op.drop_column('memories', 'importance')

"""api_keys device_user column (M40-A)

Revision ID: c1d3e5f7a9b1
Revises: a5c7e9b1d3f5
Create Date: 2026-09-22 00:00:00.000000

M40-A（docs/18 §二.5 设备-用户配对）：api_keys 加 device_user 列——Harness 设备
Key（note=harness:<name>）的归属用户标识（POST /api/v1/auth/devices body.user 显式
指定，JWT 通道缺省取 request.state.user）。「远程指令仅限该用户配对设备生效」的
配对语义地基；存量行为不变（nullable，缺省 NULL）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1d3e5f7a9b1'
down_revision: Union[str, Sequence[str], None] = 'a5c7e9b1d3f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> list[str]:
    if conn.dialect.name == "sqlite":
        return [r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()]
    return [c["name"] for c in sa.inspect(conn).get_columns(table)]


def upgrade() -> None:
    """api_keys 加 device_user 列（String(128) nullable，设备归属用户标识）；
    baseline 为 create_all 语义（新库已含该列），幂等守卫。"""
    conn = op.get_bind()
    if "device_user" not in _columns(conn, "api_keys"):
        op.add_column('api_keys', sa.Column('device_user', sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column('api_keys', 'device_user')

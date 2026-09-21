"""connectors: sql kind config + oauth credential custody + health columns

Revision ID: d7a1b3c5e9f2
Revises: c6f0a2b4d8e1
Create Date: 2026-09-21

M31 任务组 C：connector- 连接器深化——
- config JSON 列：SQL 连接器的类型专属配置（{"dialect": "sqlite", "database": "<path>"}）
- OAuth2 凭证托管列：client_id / client_secret(Fernet) / token_url / access+refresh(Fernet)
  / expires_at / scopes（client_credentials + authorization_code + refresh_token）
- Health Check 列：last_health_at / last_health_ok（POST /{name}/health 探测落库）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7a1b3c5e9f2'
down_revision: Union[str, Sequence[str], None] = 'c6f0a2b4d8e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 待补列清单：列名 → (SQLAlchemy 类型, server_default)
_NEW_COLUMNS: dict[str, tuple[sa.types.TypeEngine, str | None]] = {
    "config": (sa.JSON(), "{}"),
    "oauth_client_id": (sa.String(length=256), None),
    "oauth_client_secret_enc": (sa.String(length=512), None),
    "oauth_token_url": (sa.String(length=512), None),
    "oauth_access_token_enc": (sa.String(length=1024), None),
    "oauth_refresh_token_enc": (sa.String(length=1024), None),
    "oauth_expires_at": (sa.DateTime(), None),
    "oauth_scopes": (sa.String(length=512), None),
    "last_health_at": (sa.DateTime(), None),
    "last_health_ok": (sa.Boolean(), None),
}


def upgrade() -> None:
    """connectors 表补列。幂等（列已存在则跳过，baseline create_all 已含）。"""
    conn = op.get_bind()
    existing = {c["name"] for c in sa.inspect(conn).get_columns("connectors")}
    for name, (col_type, server_default) in _NEW_COLUMNS.items():
        if name not in existing:
            op.add_column("connectors",
                          sa.Column(name, col_type, nullable=True, server_default=server_default))


def downgrade() -> None:
    """Downgrade：回退本迁移补的列（仅删确实存在的）。"""
    conn = op.get_bind()
    existing = {c["name"] for c in sa.inspect(conn).get_columns("connectors")}
    for name in _NEW_COLUMNS:
        if name in existing:
            op.drop_column("connectors", name)

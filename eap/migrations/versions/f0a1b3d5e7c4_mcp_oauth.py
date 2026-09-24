"""MCP Server OAuth 迁移（M34/L8）：mcp_servers 加 OAuth 客户端凭证列。

模式同 M31 连接器（token_url/client_id/secret/scopes/access_token/expires_at），
secret 与 access_token Fernet 加密；幂等 add_column。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f0a1b3d5e7c4"
down_revision: Union[str, Sequence[str], None] = "e2f5a7b9c3d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

_COLUMNS = [
    ("oauth_token_url", sa.String(256), "''", False),
    ("oauth_client_id", sa.String(128), "''", False),
    ("oauth_client_secret_enc", sa.String(512), None, True),
    ("oauth_scopes", sa.String(256), "''", False),
    ("oauth_access_token_enc", sa.String(1024), None, True),
    ("oauth_expires_at", sa.DateTime(), None, True),
]


def _has_column(table: str, name: str) -> bool:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(row[1] == name for row in rows)
    # M49-C PG 实测：PRAGMA 为 SQLite 专有，其他方言走 SQLAlchemy inspector
    return any(c["name"] == name for c in sa.inspect(conn).get_columns(table))


def upgrade() -> None:
    for name, type_, default, nullable in _COLUMNS:
        if _has_column("mcp_servers", name):
            continue
        clause = f"ADD COLUMN {name} {type_.compile(dialect=op.get_bind().dialect)}"
        if default is not None:
            clause += f" NOT NULL DEFAULT {default}"
        op.execute(f"ALTER TABLE mcp_servers {clause}")


def downgrade() -> None:
    for name, _type, _default, _nullable in reversed(_COLUMNS):
        if _has_column("mcp_servers", name):
            op.execute(f"ALTER TABLE mcp_servers DROP COLUMN {name}")

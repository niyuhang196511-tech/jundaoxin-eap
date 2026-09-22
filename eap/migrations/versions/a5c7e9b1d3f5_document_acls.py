"""Document ACL 迁移（M36/L6）：document_acls 表——文档/角色级访问控制（设计 docs/17）。

deny 优先 → allow → 默认可见；document_id=None 为 KB 级默认规则。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a5c7e9b1d3f5"
down_revision: Union[str, Sequence[str], None] = "f0a1b3d5e7c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def upgrade() -> None:
    """document_acls 表（M36/L6）。已存在则幂等跳过（baseline create_all 已含）。"""
    conn = op.get_bind()
    if "document_acls" not in sa.inspect(conn).get_table_names():
        op.create_table(
            "document_acls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("kbs.id"), nullable=False, index=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=True, index=True),
            sa.Column("effect", sa.String(8), nullable=False),
            sa.Column("subject_type", sa.String(8), nullable=False),
            sa.Column("subject", sa.String(128), nullable=False),
            sa.Column("note", sa.String(256), server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if "document_acls" in sa.inspect(conn).get_table_names():
        op.drop_table("document_acls")

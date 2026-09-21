"""skill assets manifest column (M34)

Revision ID: e2f5a7b9c3d6
Revises: b8d2f4a6c0e3
Create Date: 2026-09-21 00:00:00.000000

M34（L5 工程部分）：skills 加 assets JSON 列——技能附件签名清单
[{path, size, sha256}]（随包签名覆盖，见 runtime/skill_pkg.py）；
文件本体落盘 EAP_MEDIA_DIR/skills/<name>/（runtime/skill_files.py）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e2f5a7b9c3d6'
down_revision: Union[str, Sequence[str], None] = 'c0d3e5a7f9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> list[str]:
    if conn.dialect.name == "sqlite":
        return [r[1] for r in conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()]
    return [c["name"] for c in sa.inspect(conn).get_columns(table)]


def upgrade() -> None:
    """skills 加 assets 列（清单 JSON）；baseline 为 create_all 语义（新库已含该列），幂等守卫。"""
    conn = op.get_bind()
    if "assets" not in _columns(conn, "skills"):
        op.add_column('skills', sa.Column('assets', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('skills', 'assets')

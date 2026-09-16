"""M9 RLS 迁移：postgres 方言启用行级租户隔离；SQLite 下 no-op（开发形态）。"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '6fb073d33fe8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 开发形态无 RLS 语义
    from eap.observability.rls import enable_rls

    enable_rls(op)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    from eap.observability.rls import disable_rls

    disable_rls(op)

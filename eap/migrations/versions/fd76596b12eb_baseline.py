"""baseline

Revision ID: fd76596b12eb
Revises:
Create Date: 2026-09-16 15:50:03.740386
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'fd76596b12eb'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline：从零建库（全部表，以当前模型元数据为准）。

    存量库（开发/既有部署）不执行本迁移——直接 `alembic stamp head` 对齐版本。
    后续模型变更：alembic revision --autogenerate -m "..." 生成增量迁移。
    """
    from sqlalchemy import create_engine

    from eap.config import get_settings
    from eap.models import Base

    engine = create_engine(get_settings().db_url)
    Base.metadata.create_all(engine)


def downgrade() -> None:
    """Baseline 无下级版本，downgrade 不支持。"""
    raise NotImplementedError("baseline 迁移无 downgrade")

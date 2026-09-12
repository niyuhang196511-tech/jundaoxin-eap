"""数据库：SQLAlchemy 同步引擎（开发用 SQLite，生产切 PostgreSQL + RLS，见 docs/05）。"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_settings = get_settings()

engine = create_engine(
    _settings.db_url,
    connect_args={"check_same_thread": False} if _settings.db_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import seed  # noqa: F401  确保种子逻辑可用

    Base.metadata.create_all(engine)
    seed.run(engine)
    # 注：开发版不做增量迁移——模型变更后删除 *.db 重建即可（开发数据可弃）；生产用 Alembic（docs/09）

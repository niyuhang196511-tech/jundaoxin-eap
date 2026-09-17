"""数据库：SQLAlchemy 同步引擎（开发用 SQLite，生产切 PostgreSQL + RLS，见 docs/05）。"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base as _Base

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


def _alembic_config():
    from alembic.config import Config

    # __file__ = src/eap/db.py → 项目根为其上两级（eap/，含 alembic.ini 与 migrations/）
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    cfg.set_main_option("sqlalchemy.url", _settings.db_url)
    return cfg


def _run_migrations() -> None:
    """Alembic 迁移到 head（程序化调用，等价 alembic upgrade head）。"""
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def init_db() -> None:
    """启动时建库/迁移 + 种子。

    - 空库：跑 Alembic baseline（create_all 语义）→ 种子
    - 存量库（表已存在但无 alembic_version）：stamp head 对齐（历史手工建表/手工 ALTER 的库），
      后续模型变更走 autogenerate 增量迁移
    - 已纳管库：upgrade head
    - EAP_SKIP_MIGRATIONS=1（测试子进程）：仅 create_all + seed，不走 Alembic
      （父子进程并发 upgrade 会 SQLite 锁等待）
    """
    from . import seed  # noqa: F401  确保种子逻辑可用

    if get_settings().skip_migrations:
        _Base.metadata.create_all(engine)
        seed.run(engine)
        return

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    if "alembic_version" in tables and "tenants" in tables:
        # 正常库：版本表有行 → 升级到 head；版本表为空（历史残留）→ 存量库 stamp 对齐
        from alembic import command

        with engine.connect() as conn:
            has_version_row = conn.execute(
                sa_text("SELECT count(*) FROM alembic_version")).scalar()
        if has_version_row:
            _run_migrations()
        else:
            command.stamp(_alembic_config(), "head")
    elif tables and "tenants" in tables:
        from alembic import command

        command.stamp(_alembic_config(), "head")  # 存量库（无版本表）：对齐版本号
    else:
        _run_migrations()  # 空库（或仅有残留版本表）：baseline 建全部表

    seed.run(engine)

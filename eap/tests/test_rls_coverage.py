"""M47-A：RLS 覆盖防线（成熟度盘点 P1）。

两层防线，防「新增租户表再漏配 RLS」与「豁免表语义漂移」：

1. 元数据覆盖断言——遍历 Base.metadata：凡带 tenant_id 列的表必须登记在
   rls.RLS_TABLES（启用）或 rls.RLS_EXEMPT_TABLES（显式豁免）；反向断言
   RLS_TABLES 里的表确有 tenant_id 列、豁免表确无该列（豁免表一旦加列即红，
   提醒重新评估转正——豁免只适用于无租户列或查询路径无租户语义的表）。
2. DDL 落实断言——rls.py 源码层面对 RLS_TABLES 每张表都有同款
   ENABLE/DISABLE + 策略 DDL（静态检查，PG 行为另由部署侧验证）；
   并用 fake op 直接执行 enable_rls_m47a/disable_rls_m47a 校验语句序列
   与 policies 的特例 USING（tenant_id = 0 OR ...）。

另覆盖迁移 a7c9e1f3b5d7 在 SQLite 下的 upgrade/downgrade 可执行
（PG-only DDL 在开发形态必须是无害 no-op，沿用 test_m40_receipt 的
临时库模式）。
"""

from __future__ import annotations

import os
from pathlib import Path

_EAP_DIR = Path(__file__).resolve().parents[1]  # eap/（alembic.ini、migrations/ 所在）
_RLS_SRC = _EAP_DIR / "src" / "eap" / "observability" / "rls.py"


# ---------- 防线 1：元数据覆盖（有 tenant_id 列的表 ∈ RLS ∪ 豁免） ----------

def test_rls_registry_covers_all_tenant_tables():
    """凡带 tenant_id 列的表必须登记在 RLS_TABLES ∪ RLS_EXEMPT_TABLES（防新表再漏）。"""
    from eap.models import Base
    from eap.observability.rls import RLS_EXEMPT_TABLES, RLS_TABLES

    assert not (RLS_TABLES & RLS_EXEMPT_TABLES), "启用集与豁免集不得相交"

    tenant_tables, unregistered = set(), []
    for name, table in Base.metadata.tables.items():
        if "tenant_id" in table.columns:
            tenant_tables.add(name)
            if name not in RLS_TABLES and name not in RLS_EXEMPT_TABLES:
                unregistered.append(name)
    assert not unregistered, (
        f"表 {unregistered} 带 tenant_id 列但未登记 RLS："
        "请在 eap/observability/rls.py 的 RLS_TABLES（启用）或 "
        "RLS_EXEMPT_TABLES（豁免，附理由）登记，并同步迁移 DDL")
    assert tenant_tables, "模型里应存在带 tenant_id 列的表（防元数据断言空转）"
    # 登记集不得含幽灵表（防改名/删表后清单腐化）
    assert RLS_TABLES | RLS_EXEMPT_TABLES <= set(Base.metadata.tables), \
        "RLS_TABLES/RLS_EXEMPT_TABLES 中存在 Base.metadata 之外的表"


def test_rls_exempt_tables_must_not_gain_tenant_column():
    """豁免表不得带 tenant_id 列——一旦加列即红，提醒把该表转正进 RLS_TABLES。

    （豁免的合法性建立在「无行级租户谓词可用」或「查询路径无租户语义」上，
    新增 tenant_id 列意味着前提失效，须人工重估。）
    """
    from eap.models import Base
    from eap.observability.rls import RLS_EXEMPT_TABLES

    violated = [n for n in RLS_EXEMPT_TABLES
                if "tenant_id" in Base.metadata.tables[n].columns]
    assert not violated, f"豁免表 {violated} 新增了 tenant_id 列，须重新评估并转正 RLS"


def test_rls_enabled_tables_have_tenant_column():
    """启用集里的表必须确有 tenant_id 列（RLS 谓词的载体），防清单误报。"""
    from eap.models import Base
    from eap.observability.rls import RLS_TABLES

    violated = [n for n in RLS_TABLES
                if "tenant_id" not in Base.metadata.tables[n].columns]
    assert not violated, f"RLS_TABLES 中 {violated} 无 tenant_id 列，策略谓词不成立"


# ---------- 防线 2：DDL 落实（源码静态断言 + fake op 执行） ----------

def test_rls_ddl_covers_registered_tables():
    """rls.py 源码须对 RLS_TABLES 每张表给出 ENABLE/DISABLE + 策略 DDL（登记即落实）。"""
    from eap.observability.rls import RLS_TABLES

    src = _RLS_SRC.read_text(encoding="utf-8")
    missing = [t for t in RLS_TABLES
               if f'"{t}" ENABLE ROW LEVEL SECURITY' not in src
               or f'"{t}" DISABLE ROW LEVEL SECURITY' not in src
               or f"CREATE POLICY tenant_isolation ON \"{t}\"" not in src
               or f"DROP POLICY IF EXISTS tenant_isolation ON \"{t}\"" not in src]
    assert not missing, f"rls.py 缺少表 {missing} 的 RLS DDL（启用/下线/策略）"


class _Recorder:
    """fake alembic op：只记录 execute 的 SQL，用于离线校验 DDL 序列。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str) -> None:
        self.calls.append(sql)


def test_enable_disable_rls_m47a_statement_sequence():
    """M47-A helper：三表 × (ENABLE + DROP POLICY + CREATE POLICY)，policies 为特例 USING。"""
    from eap.observability.rls import disable_rls_m47a, enable_rls_m47a

    rec = _Recorder()
    enable_rls_m47a(rec)
    # 每表三句：ENABLE / DROP POLICY IF EXISTS / CREATE POLICY
    assert len(rec.calls) == 9
    for t in ("policies", "trigger_rules", "webhook_endpoints"):
        assert f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY' in rec.calls
        assert f'DROP POLICY IF EXISTS tenant_isolation ON "{t}"' in rec.calls

    # policies 特例：本租户 OR 平台默认（tenant_id=0），与 _policies_for 回退语义对齐
    policies_using = next(c for c in rec.calls
                          if c.startswith('CREATE POLICY tenant_isolation ON "policies"'))
    assert "tenant_id = 0 OR tenant_id = current_setting('eap.tenant_id', true)::int" \
        in policies_using

    # trigger_rules / webhook_endpoints 标准式：可空列平台共享 + fail-closed
    for t in ("trigger_rules", "webhook_endpoints"):
        using = next(c for c in rec.calls
                     if c.startswith(f'CREATE POLICY tenant_isolation ON "{t}"'))
        assert "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int" \
            in using

    # 下线：每表两句（DISABLE + DROP POLICY），与启用严格对应
    rec2 = _Recorder()
    disable_rls_m47a(rec2)
    assert len(rec2.calls) == 6
    for t in ("policies", "trigger_rules", "webhook_endpoints"):
        assert f'ALTER TABLE "{t}" DISABLE ROW LEVEL SECURITY' in rec2.calls
        assert f'DROP POLICY IF EXISTS tenant_isolation ON "{t}"' in rec2.calls


# ---------- 迁移可执行（SQLite no-op 路径，沿用 test_m40_receipt 临时库模式） ----------

def _alembic(tmp_path: Path, command_name: str, target: str) -> None:
    """在独立临时库上跑 alembic 命令（env.py 经 get_settings() 读 EAP_DB_URL——
    运行期切换环境变量 + 清 lru_cache，结束恢复，不影响其他测试的库路由）。"""
    from alembic import command
    from alembic.config import Config

    from eap.config import get_settings

    db_path = tmp_path / "mig-rls.db"
    old = os.environ.get("EAP_DB_URL")
    os.environ["EAP_DB_URL"] = f"sqlite:///{db_path.as_posix()}"
    get_settings.cache_clear()
    try:
        cfg = Config()  # 不载 alembic.ini：跳过 fileConfig，避免扰动 pytest 日志配置
        cfg.set_main_option("script_location", str(_EAP_DIR / "migrations"))
        getattr(command, command_name)(cfg, target)
    finally:
        if old is None:
            os.environ.pop("EAP_DB_URL", None)
        else:
            os.environ["EAP_DB_URL"] = old
        get_settings.cache_clear()


def _version(db_path: Path) -> str:
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as conn:
            return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar())
    finally:
        engine.dispose()


def test_migration_rls_upgrade_head_on_fresh_sqlite(tmp_path):
    """全新库 upgrade head 全链通过（新迁移在 SQLite 下为无害 no-op），停在单头。"""
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory(str(_EAP_DIR / "migrations"))
    heads = sd.get_heads()
    assert len(heads) == 1, f"迁移链多头: {heads}"

    db_path = tmp_path / "mig-rls.db"
    _alembic(tmp_path, "upgrade", "head")
    assert _version(db_path) == str(heads[0])


def test_migration_rls_downgrade_then_upgrade_roundtrip(tmp_path):
    """head → downgrade -1（回 M47-A 前一版）→ upgrade head：PG-only DDL 在 SQLite
    上下行均为 no-op，链路可逆。"""
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory(str(_EAP_DIR / "migrations"))
    head = str(sd.get_heads()[0])
    prev = str(sd.get_revision(head).down_revision)  # M47-A 的父版本

    db_path = tmp_path / "mig-rls.db"
    _alembic(tmp_path, "upgrade", "head")
    assert _version(db_path) == head

    _alembic(tmp_path, "downgrade", "-1")
    assert _version(db_path) == prev

    _alembic(tmp_path, "upgrade", "head")
    assert _version(db_path) == head

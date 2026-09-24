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

M50-A 增补：策略谓词统一 NULLIF(current_setting('eap.tenant_id', true), '')::int
（防池化连接 GUC '' 残留抛错，M49-C 实测）；本文件 pin 断言同步为 NULLIF 形态，
并新增「运行时启用路径与迁移 e4a8c2f6b9d1 产出同一套谓词」的防漂移断言
（_Recorder 离线执行比对，含 downgrade 旧谓词恢复的表集对应）。

M51-C 增补（防线 3：角色收敛静态防线，SQLite 也须绿）：
- 迁移 c7e9b2d4f6a8 源码断言——eap_app NOLOGIN 角色幂等创建（pg_roles 守卫）、
  应用级 DML GRANT（表/序列/schema/默认权限）、方言守卫、downgrade 对称回收
  （成员关系 → DROP OWNED → DROP ROLE）与迁移链单头衔接；
- deps.py 源码断言——SET LOCAL ROLE 位于 set_config('eap.tenant_id', ...) 之后、
  postgresql 方言守卫之内，且经 _APP_ROLE_RE 白名单校验（SET ROLE 不支持绑定
  参数，角色名字面量内联必须先过 ^[a-z_][a-z0-9_]{0,62}$）；
- 白名单校验函数的行为级单测（合法角色名 → 静态字面量语句；注入形态一律 ValueError）。
PG 行为实测（HTTP 全链路 + '' 残留组合）见 tests/test_rls_behavior.py 的 M51-C 段。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_EAP_DIR = Path(__file__).resolve().parents[1]  # eap/（alembic.ini、migrations/ 所在）
_RLS_SRC = _EAP_DIR / "src" / "eap" / "observability" / "rls.py"
_DEPS_SRC = _EAP_DIR / "src" / "eap" / "api" / "deps.py"
_M51C_MIGRATION = _EAP_DIR / "migrations" / "versions" / "c7e9b2d4f6a8_rls_app_role.py"


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
    # （M50-A：NULLIF 形态——池化连接 GUC '' 残留时得 NULL，仍见平台默认行，见 rls.py 头注释）
    policies_using = next(c for c in rec.calls
                          if c.startswith('CREATE POLICY tenant_isolation ON "policies"'))
    assert "tenant_id = 0 OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int" \
        in policies_using

    # trigger_rules / webhook_endpoints 标准式：可空列平台共享 + fail-closed（NULLIF 形态）
    for t in ("trigger_rules", "webhook_endpoints"):
        using = next(c for c in rec.calls
                     if c.startswith(f'CREATE POLICY tenant_isolation ON "{t}"'))
        assert "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int" \
            in using

    # 下线：每表两句（DISABLE + DROP POLICY），与启用严格对应
    rec2 = _Recorder()
    disable_rls_m47a(rec2)
    assert len(rec2.calls) == 6
    for t in ("policies", "trigger_rules", "webhook_endpoints"):
        assert f'ALTER TABLE "{t}" DISABLE ROW LEVEL SECURITY' in rec2.calls
        assert f'DROP POLICY IF EXISTS tenant_isolation ON "{t}"' in rec2.calls


# ---------- M50-A：NULLIF 谓词形态 + 运行时启用路径与迁移防漂移 ----------

_NULLIF_PREDICATE = "NULLIF(current_setting('eap.tenant_id', true), '')::int"
_LEGACY_PREDICATE = "current_setting('eap.tenant_id', true)::int"


def _creates(rec: _Recorder) -> list[str]:
    return [c for c in rec.calls if c.startswith("CREATE POLICY tenant_isolation ON ")]


def _policy_tables(rec: _Recorder) -> set[str]:
    return {c.split(' ON "')[1].split('"')[0] for c in _creates(rec)}


def test_m50a_migration_and_runtime_predicates_in_sync():
    """M50-A 防漂移：迁移 e4a8c2f6b9d1 的 upgrade 路径（recreate_policies_m50a）
    与运行时启用路径（enable_rls + enable_rls_m47a）产出的 CREATE POLICY 语句
    逐条相同，且全部 11 张 RLS_TABLES 均为 NULLIF 新谓词（旧谓词 ::int 对池化
    连接的 GUC '' 残留抛 InvalidTextRepresentation——M49-C 实测，见 rls.py 头注释）。
    downgrade 路径（restore_policies_pre_m50a）恢复旧谓词、覆盖同一表集（可逆）。"""
    from eap.observability.rls import (
        RLS_TABLES,
        enable_rls,
        enable_rls_m47a,
        recreate_policies_m50a,
        restore_policies_pre_m50a,
    )

    runtime = _Recorder()
    enable_rls(runtime)
    enable_rls_m47a(runtime)
    migration = _Recorder()
    recreate_policies_m50a(migration)

    rt, mg = _creates(runtime), _creates(migration)
    assert rt == mg, "迁移 e4a8c2f6b9d1 与 rls.py 启用路径的谓词漂移（两处必须同步）"
    assert _policy_tables(runtime) == set(RLS_TABLES), \
        "CREATE POLICY 覆盖表集与 RLS_TABLES 不一致"
    assert all(_NULLIF_PREDICATE in c for c in rt), \
        "存在未换 NULLIF 形态的策略谓词（'' 残留 GUC 下 ::int 抛错）"

    legacy = _Recorder()
    restore_policies_pre_m50a(legacy)
    lg = _creates(legacy)
    assert _policy_tables(legacy) == set(RLS_TABLES), \
        "downgrade 恢复的表集与 upgrade 不一致（迁移必须可逆）"
    assert all("NULLIF" not in c for c in lg), "downgrade 应恢复 M50-A 之前的旧谓词"
    assert all(_LEGACY_PREDICATE in c for c in lg)
    # 每表 DROP 与 CREATE 成对（drop + recreate 语义，防旧策略残留）
    drops = [c for c in legacy.calls if c.startswith("DROP POLICY IF EXISTS tenant_isolation")]
    assert len(drops) == len(lg) == len(RLS_TABLES)


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
    """head → downgrade -1（回 head 前一版）→ upgrade head：PG-only DDL 在 SQLite
    上下行均为 no-op，链路可逆。"""
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory(str(_EAP_DIR / "migrations"))
    head = str(sd.get_heads()[0])
    prev = str(sd.get_revision(head).down_revision)  # head 的父版本

    db_path = tmp_path / "mig-rls.db"
    _alembic(tmp_path, "upgrade", "head")
    assert _version(db_path) == head

    _alembic(tmp_path, "downgrade", "-1")
    assert _version(db_path) == prev

    _alembic(tmp_path, "upgrade", "head")
    assert _version(db_path) == head


# ---------- M51-C 防线 3：角色收敛（迁移 DDL / deps.py SET LOCAL ROLE 静态断言） ----------

def test_m51c_migration_creates_app_role_with_grants():
    """迁移 c7e9b2d4f6a8 源码断言：eap_app NOLOGIN 角色幂等创建 + 应用级 DML GRANT
    全量存在（表/序列/schema/默认权限），方言守卫与 downgrade 对称回收齐备。
    行为实测（GRANT 完整性门禁）见 test_rls_behavior.py 的 HTTP 全链路用例。"""
    from alembic.script import ScriptDirectory

    # 迁移链衔接：接 M50-A head（e4a8c2f6b9d1），单头线性（多头即红）
    sd = ScriptDirectory(str(_EAP_DIR / "migrations"))
    assert len(sd.get_heads()) == 1, f"迁移链多头: {sd.get_heads()}"
    assert str(sd.get_revision("c7e9b2d4f6a8").down_revision) == "e4a8c2f6b9d1"

    src = _M51C_MIGRATION.read_text(encoding="utf-8")
    # 方言守卫（SQLite no-op，upgrade/downgrade 两侧）
    assert src.count('dialect.name != "postgresql"') == 2, "upgrade/downgrade 均须方言守卫"
    # 幂等建角色：pg_roles 不存在才 CREATE（DO $$ 块），NOLOGIN
    assert "pg_roles WHERE rolname = 'eap_app'" in src
    assert "CREATE ROLE eap_app NOLOGIN" in src
    # GRANT 盘点结论的落地（应用级 DML 全量；租户隔离交给 RLS 而非表级裁剪）
    assert "GRANT USAGE ON SCHEMA public TO eap_app" in src
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO eap_app" in src
    assert "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO eap_app" in src
    # 未来新建表/序列自动携带同款授权（防后续建表迁移漏 GRANT → 运行时 42501）
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in src
    # downgrade 对称回收：成员关系遍历 + 对象归属防御检查 + DROP OWNED + DROP ROLE
    for stmt in ("pg_auth_members", "REVOKE eap_app FROM", "relowner = 'eap_app'::regrole",
                 "DROP OWNED BY eap_app", "DROP ROLE eap_app"):
        assert stmt in src, f"downgrade 缺少 {stmt!r}（回收不完整会阻塞 DROP ROLE 或残留授权）"


def test_m51c_deps_set_local_role_after_set_config_with_whitelist():
    """deps.py 源码断言：JWT 通道的 SET LOCAL ROLE 必须
    ① 在 postgresql 方言守卫之内、② 位于 set_config('eap.tenant_id', ...) 之后
    （先设 GUC 再收敛角色，与迁移 c7e9b2d4f6a8 的授权配套）、
    ③ 经 settings.db_app_role 开关（空 = 关闭，向后兼容）、
    ④ 角色名过 _APP_ROLE_RE 白名单（SET ROLE 不支持绑定参数，字面量内联的唯一
    零注入面写法——对齐 rls.py 的 DDL 静态字面量纪律）。"""
    src = _DEPS_SRC.read_text(encoding="utf-8")
    i_dialect = src.index('dialect.name == "postgresql"')
    i_set_config = src.index("set_config('eap.tenant_id', :t, true)")
    i_gate = src.index("get_settings().db_app_role")
    i_role = src.index("_set_local_role_sql(app_role)")
    assert i_dialect < i_set_config < i_gate < i_role, (
        "SET LOCAL ROLE 必须在 postgresql 守卫内、set_config 之后、经 db_app_role 开关")
    # 白名单正则与 SET LOCAL ROLE 字面量构造同在 deps.py（防校验被绕过/正则被弱化）
    assert r're.compile(r"^[a-z_][a-z0-9_]{0,62}$")' in src
    # 语句构造点唯一（仅 _set_local_role_sql 一处内联角色名；API Key/embed/worker 路径零改动）
    assert src.count('return f"SET LOCAL ROLE {role}"') == 1


def test_m51c_app_role_whitelist_validation_behavior():
    """白名单校验的行为级单测：合法 PG 常规标识符 → 静态字面量语句；
    大写/连字符/空格/分号注入/数字开头/超长（>63 字节 NAMEDATALEN）/空串一律 ValueError
    （fail-closed，resolve_tenant 转 500——绝不静默跳过回退 owner）。"""
    from eap.api.deps import _set_local_role_sql

    assert _set_local_role_sql("eap_app") == "SET LOCAL ROLE eap_app"
    assert _set_local_role_sql("_a1") == "SET LOCAL ROLE _a1"
    assert _set_local_role_sql("a" * 63) == f"SET LOCAL ROLE {'a' * 63}"

    for bad in ("", "EAP_APP", "eap-app", "eap app", "1eap", "eap;DROP ROLE x",
                "eap_app--", "a" * 64, "eap\napp", "eap.app", '"eap_app"',
                "eap_app TO PUBLIC"):
        with pytest.raises(ValueError):
            _set_local_role_sql(bad)

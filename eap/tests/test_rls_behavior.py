"""M49-C：RLS 行为级实测（成熟度盘点 P2 #22）——真实 PostgreSQL 上行级租户隔离生效验证。

与 tests/test_rls_coverage.py 的分工：那边是元数据/DDL 静态防线（SQLite 也能跑），
本文件是 PG-only 行为实测（dialect != postgresql 时整文件 skip）：

- 策略生效：SET LOCAL eap.tenant_id=A 的租户会话只见 A 行（+平台共享行），B 行不可见；
- policies 特例：USING tenant_id = 0 OR tenant_id = current——租户会话同时可见
  「本租户策略」与「tenant_id=0 平台默认策略」（runtime/policy.py::_policies_for 回退依据）；
- fail-closed：未设 eap.tenant_id 的受限会话看不到任何租户行（仅平台共享行可见）；
- M50-A 回归：池化连接 '' 残留 GUC（M49-C 实测的常态）下查询不抛错（NULLIF 加固）
  且同样 fail-closed（policies 特例仍见 tenant_id=0 平台默认行）；
- 豁免表：tasks（RLS_EXEMPT_TABLES，租户上下文在 payload JSON 内）不受影响，仍全见；
- 平台身份：API Key 通道语义（表 owner / BYPASSRLS，见 rls.py 头注释）不受 RLS 约束。

角色语义说明（与生产一致）：
- rls.py 启用 RLS 时**不用** FORCE ROW LEVEL SECURITY；PostgreSQL 对超级用户与表 owner
  默认不生效。部署形态（postgres:16-alpine，POSTGRES_USER=eap）中应用连接即超级用户
  +owner，正是 rls.py 注释的「API Key 通道为平台运维身份（app role 为表 owner /
  BYPASSRLS），不受 RLS 约束」；JWT 通道的租户隔离依赖非超级用户/非 owner 的应用角色
  （角色管理属部署侧职责）。
- 因此本测试建一个 NOLOGIN 探针角色 eap_rls_probe（非超级用户、非表 owner，仅授 SELECT），
  在事务内 SET LOCAL ROLE 切入该身份，模拟 JWT 租户通道的受限会话；
  eap.tenant_id 用与 deps.resolve_tenant 完全相同的参数化 set_config(..., is_local=true)
  （SET 语句不支持绑定参数，M49-C 修正后生产同款写法）。

会话状态注意事项（M49-C 实测发现的 PG 占位 GUC 行为；M50-A 已加固）：
- 会话一旦设置过 eap.tenant_id（含事务内 SET LOCAL），事务结束后该自定义占位变量
  残留为空串 ''（而非「未设置」）：current_setting(..., true) 返回 ''（set_config(NULL)
  /RESET 同样落到 ''）。旧谓词 ::int 对 '' 抛 InvalidTextRepresentation——连接池复用
  的连接因此无法忠实呈现「未设置该变量」的 fail-closed 语义（那要求 GUC 真未设置 → NULL）。
- M50-A（迁移 e4a8c2f6b9d1）已将全部策略谓词改为
  NULLIF(current_setting('eap.tenant_id', true), '')::int：'' 残留与未设置一律得
  NULL → 租户行不可见，fail-closed 语义对池化连接同样成立。本文件末两条
  test_empty_guc_residue_* 为该修复的行为级回归（'' 残留下不抛错 + 只见平台/共享行）。
- 「GUC 真未设置（NULL）」的 fail-closed 断言仍用 NullPool 全新会话 + 已提交探针数据
  （用后即删）；其余断言全部在事务内完成并回滚，对共享测试库零残留。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.pool import NullPool

from eap.db import engine

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="RLS 行为实测仅 PostgreSQL（SQLite 无 RLS 语义，静态防线见 test_rls_coverage.py）",
)

_PROBE_ROLE = "eap_rls_probe"

# 探针数据（前缀隔离于种子/其他测试数据；事务内插入 + 回滚，fail-closed 用后即删）
_TENANT_A, _TENANT_B = 990001, 990002
_POLICY_A, _POLICY_B, _POLICY_PLATFORM = (
    "rls-probe-pol-a", "rls-probe-pol-b", "rls-probe-pol-platform")
_TRIG_A, _TRIG_B, _TRIG_SHARED = (
    "rls-probe-trig-a", "rls-probe-trig-b", "rls-probe-trig-shared")
_HOOK_A, _HOOK_B, _HOOK_SHARED = (
    "rls-probe-hook-a", "rls-probe-hook-b", "rls-probe-hook-shared")
_TASK_1, _TASK_2 = "rls-probe-task-1", "rls-probe-task-2"
_PREFIX = "rls-probe-%"
# fail-closed 专用（须提交才对新会话可见；event_type/events 用无害占位，
# 避免短暂存在期间被触发器/webhook 引擎的周期性 reload 拾取执行）
_FC_POLICY_A, _FC_POLICY_PLATFORM = "rls-probe-fc-pol-a", "rls-probe-fc-pol-platform"
_FC_TRIG_A, _FC_TRIG_SHARED = "rls-probe-fc-trig-a", "rls-probe-fc-trig-shared"
_FC_HOOK_A, _FC_HOOK_SHARED = "rls-probe-fc-hook-a", "rls-probe-fc-hook-shared"
_FC_PREFIX = "rls-probe-fc-%"
# M50-A '' 残留回归专用（同 fail-closed：须提交才对 NullPool 新连接可见；用后即删）
_RS_POLICY_A, _RS_POLICY_PLATFORM = "rls-probe-rs-pol-a", "rls-probe-rs-pol-platform"
_RS_TRIG_A, _RS_TRIG_SHARED = "rls-probe-rs-trig-a", "rls-probe-rs-trig-shared"
_RS_HOOK_A, _RS_HOOK_SHARED = "rls-probe-rs-hook-a", "rls-probe-rs-hook-shared"
_RS_PREFIX = "rls-probe-rs-%"

_INSERT_POLICY = sa_text(
    "INSERT INTO policies (name, tenant_id, kind, config, enabled, priority,"
    " notes, created_at) VALUES (:n, :t, 'model-allowlist', '{}'::json, true,"
    " 100, '', now())")
_INSERT_TRIGGER = sa_text(
    "INSERT INTO trigger_rules (name, tenant_id, source, event_type,"
    " target_type, target_name, input_mode, min_interval_s, enabled,"
    " created_at, updated_at) VALUES (:n, :t, 'event', :ev,"
    " 'agent', 'faq', 'payload', 0, true, now(), now())")
_INSERT_HOOK = sa_text(
    "INSERT INTO webhook_endpoints (name, url, events, tenant_id, enabled,"
    " created_at, updated_at) VALUES (:n, 'https://probe.invalid/hook', :evs,"
    " :t, true, now(), now())")


@pytest.fixture(scope="module", autouse=True)
def _rls_env(client):
    """模块级 RLS 实测环境。

    依赖 session 级 client 夹具：先触发 create_app lifespan（建库/迁移到 head——
    RLS 由迁移 a1f2c3d4e5f6 / a7c9e1f3b5d7 在 postgres 方言启用）再建探针角色。
    """
    with engine.connect() as conn:
        if conn.execute(sa_text(
                "SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _PROBE_ROLE}).scalar():
            # 前次异常残留：先解除依赖再删（DROP ROLE 对仍持授权的角色会失败）
            conn.execute(sa_text(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {_PROBE_ROLE}"))
            conn.execute(sa_text(f"REVOKE ALL ON SCHEMA public FROM {_PROBE_ROLE}"))
            conn.execute(sa_text(f"DROP ROLE {_PROBE_ROLE}"))
        # NOLOGIN：仅经 SET [LOCAL] ROLE 从超级用户会话切入（无需密码/pg_hba 变更）
        conn.execute(sa_text(f"CREATE ROLE {_PROBE_ROLE} NOLOGIN"))
        conn.execute(sa_text(f"GRANT USAGE ON SCHEMA public TO {_PROBE_ROLE}"))
        conn.execute(sa_text(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_PROBE_ROLE}"))
        conn.commit()
    yield
    with engine.connect() as conn:
        conn.execute(sa_text(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {_PROBE_ROLE}"))
        conn.execute(sa_text(f"REVOKE ALL ON SCHEMA public FROM {_PROBE_ROLE}"))
        conn.execute(sa_text(f"DROP ROLE IF EXISTS {_PROBE_ROLE}"))
        conn.commit()


@pytest.fixture()
def seeded():
    """事务内播种探针数据 → 测试 → 回滚（对共享测试库零残留）。

    播种以平台身份（超级用户 eap）执行；RLS 断言经 _as_tenant 切入探针角色。
    """
    with engine.connect() as conn:
        trans = conn.begin()
        # policies：tenant_id NOT NULL（0=平台默认）；config 为 JSON 列
        for name, tid in ((_POLICY_A, _TENANT_A), (_POLICY_B, _TENANT_B),
                          (_POLICY_PLATFORM, 0)):
            conn.execute(_INSERT_POLICY, {"n": name, "t": tid})
        # trigger_rules / webhook_endpoints：tenant_id 可空（NULL=平台共享）
        for name, tid in ((_TRIG_A, _TENANT_A), (_TRIG_B, _TENANT_B),
                          (_TRIG_SHARED, None)):
            conn.execute(_INSERT_TRIGGER,
                         {"n": name, "t": tid, "ev": "agent.run.completed"})
        for name, tid in ((_HOOK_A, _TENANT_A), (_HOOK_B, _TENANT_B),
                          (_HOOK_SHARED, None)):
            conn.execute(_INSERT_HOOK, {"n": name, "t": tid, "evs": "[]"})
        # tasks：豁免表（无 tenant_id 列），租户上下文在 payload JSON 内
        for task_id, tid in ((_TASK_1, _TENANT_A), (_TASK_2, _TENANT_B)):
            conn.execute(sa_text(
                "INSERT INTO tasks (id, type, state, payload, result, priority,"
                " created_at, updated_at) VALUES (:i, 'generic', 'PENDING',"
                " jsonb_build_object('_tenant_id', :t), '{}'::json, 0, now(), now())"),
                {"i": task_id, "t": tid})
        yield conn
        trans.rollback()


def _as_tenant(conn, tenant_id: int) -> None:
    """事务内切入租户 JWT 身份：SET LOCAL ROLE（受限探针）+ 会话变量 set_config。

    set_config('eap.tenant_id', :t, true) 与 api/deps.py::resolve_tenant 的 JWT
    通道完全一致（SET 语句不支持绑定参数，生产同款写法），事务结束自动还原。
    """
    conn.execute(sa_text(f"SET LOCAL ROLE {_PROBE_ROLE}"))
    conn.execute(sa_text("SELECT set_config('eap.tenant_id', :t, true)"),
                 {"t": str(tenant_id)})


def _visible(conn, sql: str) -> set[str]:
    return {r[0] for r in conn.execute(sa_text(sql)).fetchall()}


def _rows(conn, table: str, column: str, prefix: str = _PREFIX) -> set[str]:
    # 表名/列名为本文件内静态字面量（同 rls.py 的 DDL 纪律，零注入面）
    return _visible(conn, f'SELECT "{column}" FROM "{table}" WHERE "{column}" LIKE \'{prefix}\'')


# ---------- RLS 已启用（迁移落实）与豁免表未启用 ----------

def test_rls_enabled_on_registry_tables_not_on_exempt(seeded):
    """迁移已对 policies/trigger_rules/webhook_endpoints 启用 RLS（relrowsecurity），
    豁免表 tasks 未启用；且与 rls.py 一致——未用 FORCE（owner 豁免语义由角色管理承担）。"""
    rows = dict(seeded.execute(sa_text(
        "SELECT relname, relrowsecurity FROM pg_class"
        " WHERE relname IN ('policies', 'trigger_rules', 'webhook_endpoints', 'tasks')"
    )).fetchall())
    assert rows == {"policies": True, "trigger_rules": True,
                    "webhook_endpoints": True, "tasks": False}

    forced = seeded.execute(sa_text(
        "SELECT count(*) FROM pg_class WHERE relname IN"
        " ('policies', 'trigger_rules', 'webhook_endpoints') AND relforcerowsecurity"
    )).scalar()
    assert forced == 0, "rls.py 不启用 FORCE ROW LEVEL SECURITY（部署语义变更须同步本测试）"

    tables = {r[0] for r in seeded.execute(sa_text(
        "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'"
    )).fetchall()}
    assert {"policies", "trigger_rules", "webhook_endpoints"} <= tables


# ---------- policies：特例 USING（本租户 OR tenant_id=0 平台默认） ----------

def test_policies_tenant_a_sees_own_and_platform_default(seeded):
    """租户 A 会话：见 A 策略 + tenant_id=0 平台默认（回退依据），不见 B 策略。"""
    _as_tenant(seeded, _TENANT_A)
    assert _rows(seeded, "policies", "name") == {_POLICY_A, _POLICY_PLATFORM}


def test_policies_tenant_b_symmetric(seeded):
    """租户 B 会话对称：见 B + 平台默认，不见 A（跨租户零泄露）。"""
    _as_tenant(seeded, _TENANT_B)
    assert _rows(seeded, "policies", "name") == {_POLICY_B, _POLICY_PLATFORM}


# ---------- trigger_rules / webhook_endpoints：标准式（NULL=平台共享） ----------

def test_trigger_rules_tenant_isolation(seeded):
    """租户 A 会话：见 A 规则 + NULL 平台共享规则，不见 B 规则。"""
    _as_tenant(seeded, _TENANT_A)
    assert _rows(seeded, "trigger_rules", "name") == {_TRIG_A, _TRIG_SHARED}


def test_webhook_endpoints_tenant_isolation(seeded):
    """租户 A 会话：见 A 端点 + NULL 平台共享端点，不见 B 端点。"""
    _as_tenant(seeded, _TENANT_A)
    assert _rows(seeded, "webhook_endpoints", "name") == {_HOOK_A, _HOOK_SHARED}


# ---------- fail-closed：未设会话变量的全新受限会话 ----------

def test_fail_closed_on_fresh_session_without_tenant_var():
    """fail-closed（rls.py 头注释语义）：GUC 真未设置（全新会话，current_setting →
    NULL）的受限会话看不到任何租户行——policies 仅 tenant_id=0 平台默认可见
    （特例 USING 无 IS NULL 分支），trigger_rules/webhook_endpoints 仅 NULL 平台共享行。

    数据须已提交才对全新会话可见：播种用无害 event_type/events（不被引擎执行），
    finally 全删；探针会话经 NullPool 独立建连（绕开连接池的 GUC 残留，见模块注释）。
    """
    with engine.begin() as conn:  # 平台身份播种（提交）
        conn.execute(_INSERT_POLICY, {"n": _FC_POLICY_A, "t": _TENANT_A})
        conn.execute(_INSERT_POLICY, {"n": _FC_POLICY_PLATFORM, "t": 0})
        conn.execute(_INSERT_TRIGGER, {"n": _FC_TRIG_A, "t": _TENANT_A,
                                       "ev": "rls.probe.none"})
        conn.execute(_INSERT_TRIGGER, {"n": _FC_TRIG_SHARED, "t": None,
                                       "ev": "rls.probe.none"})
        conn.execute(_INSERT_HOOK, {"n": _FC_HOOK_A, "t": _TENANT_A,
                                    "evs": '["rls.probe.none"]'})
        conn.execute(_INSERT_HOOK, {"n": _FC_HOOK_SHARED, "t": None,
                                    "evs": '["rls.probe.none"]'})
    fresh = create_engine(engine.url, poolclass=NullPool)
    try:
        with fresh.connect() as c:
            # 全新会话：eap.tenant_id 从未设置（NULL）；SET ROLE（会话级，随连接关闭消失）
            c.execute(sa_text(f"SET ROLE {_PROBE_ROLE}"))
            assert _rows(c, "policies", "name", _FC_PREFIX) == {_FC_POLICY_PLATFORM}
            assert _rows(c, "trigger_rules", "name", _FC_PREFIX) == {_FC_TRIG_SHARED}
            assert _rows(c, "webhook_endpoints", "name", _FC_PREFIX) == {_FC_HOOK_SHARED}
    finally:
        fresh.dispose()
        with engine.begin() as conn:  # 清理已提交探针数据（用后即删，零残留）
            for table in ("policies", "trigger_rules", "webhook_endpoints"):
                conn.execute(sa_text(
                    f'DELETE FROM "{table}" WHERE name LIKE :p'), {"p": _FC_PREFIX})


# ---------- 豁免表：tasks 不受 RLS 影响 ----------

def test_exempt_table_tasks_visible_to_all_tenants(seeded):
    """豁免表 tasks（RLS_EXEMPT_TABLES）：租户 A/B 会话均全见（列级 RLS 无行级谓词，
    租户上下文在 payload._tenant_id，由应用层过滤）。"""
    _as_tenant(seeded, _TENANT_A)
    assert _rows(seeded, "tasks", "id") == {_TASK_1, _TASK_2}

    # 同事务再切租户 B（set_config local 覆盖会话变量；角色已是探针，重设无副作用）
    seeded.execute(sa_text("SELECT set_config('eap.tenant_id', :t, true)"),
                   {"t": str(_TENANT_B)})
    assert _rows(seeded, "tasks", "id") == {_TASK_1, _TASK_2}


# ---------- 平台身份（API Key 通道）：owner/BYPASSRLS 不受约束 ----------

def test_platform_identity_bypasses_rls(seeded):
    """API Key 通道语义（rls.py 头注释）：平台运维身份（超级用户/表 owner）不受
    RLS 约束——不 SET ROLE 的会话（部署形态的应用连接）A/B/平台行全见。"""
    assert _rows(seeded, "policies", "name") == {_POLICY_A, _POLICY_B, _POLICY_PLATFORM}
    assert _rows(seeded, "trigger_rules", "name") == {_TRIG_A, _TRIG_B, _TRIG_SHARED}
    assert _rows(seeded, "webhook_endpoints", "name") == {_HOOK_A, _HOOK_B, _HOOK_SHARED}


# ---------- M50-A 回归：'' 残留 GUC（池化连接常态）不抛错且 fail-closed ----------

@pytest.fixture()
def residue_conn():
    """已提交探针数据 + 制造 '' 残留 GUC 的 NullPool 连接（yield 后数据即删，零残留）。

    残留制造方式与生产池化连接同路径：事务内 set_config('eap.tenant_id','1',true)
    （= SET LOCAL，deps.resolve_tenant 同款写法）→ 提交结束事务——M49-C psql 实测：
    事务结束后该占位 GUC 在连接上残留为 ''（而非「未设置」）。随后 SET ROLE 探针
    进入受限身份；fixture 内自证断言 current_setting → ''（若 PG 行为变化会先红在这里，
    而非误判为 NULLIF 修复失效）。旧谓词（无 NULLIF）在此状态下 ::int 抛
    InvalidTextRepresentation——正是本回归防线守护的场景。
    """
    with engine.begin() as conn:  # 平台身份播种（提交，对 NullPool 新连接可见）
        conn.execute(_INSERT_POLICY, {"n": _RS_POLICY_A, "t": _TENANT_A})
        conn.execute(_INSERT_POLICY, {"n": _RS_POLICY_PLATFORM, "t": 0})
        conn.execute(_INSERT_TRIGGER, {"n": _RS_TRIG_A, "t": _TENANT_A,
                                       "ev": "rls.probe.none"})
        conn.execute(_INSERT_TRIGGER, {"n": _RS_TRIG_SHARED, "t": None,
                                       "ev": "rls.probe.none"})
        conn.execute(_INSERT_HOOK, {"n": _RS_HOOK_A, "t": _TENANT_A,
                                    "evs": '["rls.probe.none"]'})
        conn.execute(_INSERT_HOOK, {"n": _RS_HOOK_SHARED, "t": None,
                                    "evs": '["rls.probe.none"]'})
    fresh = create_engine(engine.url, poolclass=NullPool)
    try:
        with fresh.connect() as c:
            with c.begin():  # 事务内 SET LOCAL 等价写法 → 提交，制造 '' 残留
                c.execute(sa_text("SELECT set_config('eap.tenant_id', '1', true)"))
            assert c.execute(sa_text(
                "SELECT current_setting('eap.tenant_id', true)")).scalar() == "", \
                "前置条件失效：事务结束后 GUC 应残留为 ''（M49-C 实测行为）"
            c.execute(sa_text(f"SET ROLE {_PROBE_ROLE}"))  # 会话级，随连接关闭消失
            yield c
    finally:
        fresh.dispose()
        with engine.begin() as conn:  # 清理已提交探针数据（用后即删，零残留）
            for table in ("policies", "trigger_rules", "webhook_endpoints"):
                conn.execute(sa_text(
                    f'DELETE FROM "{table}" WHERE name LIKE :p'), {"p": _RS_PREFIX})


def test_empty_guc_residue_fail_closed_standard_tables(residue_conn):
    """M50-A 回归：'' 残留 GUC 的受限会话查询标准式 RLS 表**不抛错**（NULLIF 加固，
    旧谓词此处抛 InvalidTextRepresentation）且 fail-closed——只见 NULL 平台共享行，
    租户 A 行不可见（'' → NULL → 比较为 NULL，与 GUC 真未设置语义一致）。"""
    assert _rows(residue_conn, "trigger_rules", "name", _RS_PREFIX) == {_RS_TRIG_SHARED}
    assert _rows(residue_conn, "webhook_endpoints", "name", _RS_PREFIX) == {_RS_HOOK_SHARED}


def test_empty_guc_residue_policies_platform_default(residue_conn):
    """M50-A 回归：'' 残留下 policies 特例（tenant_id = 0 OR tenant_id = current）
    不抛错，且仍见 tenant_id=0 平台默认行、不见租户 A 行——平台默认回退语义
    （runtime/policy.py::_policies_for）在池化连接残留状态下保持不变。"""
    assert _rows(residue_conn, "policies", "name", _RS_PREFIX) == {_RS_POLICY_PLATFORM}

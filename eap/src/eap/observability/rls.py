"""PostgreSQL RLS（M9，docs/05 §5 的落地）：行级租户隔离的可选启用。

设计：
- 迁移（postgres 方言）里对带 tenant_id 的表启用 RLS + 策略：
    tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int
  （NULL = 平台共享，全租户可见）
  SQLite 下整个迁移为 no-op（开发形态无 RLS 语义）。
- 应用侧：JWT 通道请求开始时 SET LOCAL eap.tenant_id=<租户>（deps.resolve_tenant 内），
  API Key 通道为平台运维身份（app role 为表 owner / BYPASSRLS），不受 RLS 约束——
  与"API Key=服务间全量、JWT=租户用户"的凭证模型一致。
- 表清单与 models.py 的 tenant_id 列保持同步（新增租户表时在此登记）。

M50-A NULLIF 加固（「收敛为非 owner 应用角色」的前置条件，存量库经迁移
e4a8c2f6b9d1 drop + recreate 全部策略）：
- M49-C PG 实测：池化连接一旦设置过 eap.tenant_id（SET LOCAL 或 set_config(..., true)），
  事务结束后该占位 GUC 在连接上**残留为空串 ''** 而非「未设置」（RESET /
  set_config(NULL) 同样落 ''）。旧谓词 current_setting('eap.tenant_id', true)::int
  对 '' 抛 InvalidTextRepresentation——连接池复用后租户通道查询整体报错。
  当前部署形态（应用以表 owner/超级用户连接，RLS 天然豁免不评估谓词）不可达，
  但收敛为非 owner 应用角色后必踩中。
- NULLIF(current_setting('eap.tenant_id', true), '')::int：GUC 为 ''（残留）或
  未设置时一律得 NULL，比较为 NULL → 租户行不可见，与既有 fail-closed 语义
  （GUC 未设置只见平台/共享行）一致；policies 特例（tenant_id = 0 OR
  tenant_id = current）在 NULL 时仍见平台默认行，回退语义不变。
- 运行时启用路径（enable_rls / enable_rls_m47a）与迁移 e4a8c2f6b9d1
  （recreate_policies_m50a 复用前两者）共用同一套字面量谓词，
  tests/test_rls_coverage.py 设防漂移断言。

M51-C 角色收敛已可启用（策略从「owner 豁免、形同虚设」变为 JWT 通道真实生效）：
- 迁移 c7e9b2d4f6a8 幂等创建 NOLOGIN 应用角色 eap_app 并授应用级 DML
  （全量 public 表 SELECT/INSERT/UPDATE/DELETE + 序列 + 默认权限，盘点口径见该迁移
  docstring——租户隔离交给本文件的 RLS 策略而非表级 GRANT 裁剪）。
- 启用：设 EAP_DB_APP_ROLE=eap_app 并重启（api/deps.py 的 JWT 通道在 set_config
  之后同事务内 SET LOCAL ROLE，角色名经 ^[a-z_][a-z0-9_]{0,62}$ 白名单校验；
  commit/rollback 后自动恢复登录角色，事务级作用域与 GUC 一致）。留空 = 关闭，
  行为与历史完全一致。
- 平台身份豁免路径不变：API Key/embed/worker 与 trigger/webhook 引擎的独立会话
  reload() 仍走登录角色（部署形态 = 表 owner/超级用户，RLS 天然豁免、全量可见）。
- 本文件仍刻意不用 FORCE ROW LEVEL SECURITY——收敛靠「SET LOCAL ROLE 到非 owner
  角色」而非「FORCE 到 owner」，平台身份语义零改动（test_rls_behavior 设防）。

安全说明：DDL 全部为逐表静态字面量、内联于 execute 调用（无运行时拼接/格式化/变量传递）——
PostgreSQL 不支持标识符参数化，静态枚举是 DDL 场景下唯一的零注入面写法。

注意：策略按 `eap.tenant_id` 会话变量生效；该变量未设置（或池化连接残留 ''）且非
BYPASSRLS 角色将看不到任何租户行（fail-closed），避免漏配导致跨租户泄露。
"""

from __future__ import annotations

# RLS 注册表（测试防线的权威清单，tests/test_rls_coverage.py 据此断言）：
# 凡带 tenant_id 列的表必须登记在 RLS_TABLES（启用）或 RLS_EXEMPT_TABLES（豁免）。
RLS_TABLES: frozenset[str] = frozenset({
    # M9 八表（迁移 a1f2c3d4e5f6，enable_rls）
    "users", "api_keys", "models", "kbs", "usage_records", "budgets",
    "memories", "workflows",
    # M47-A 补齐三表（迁移 a7c9e1f3b5d7，enable_rls_m47a）
    "policies", "trigger_rules", "webhook_endpoints",
})

# 显式豁免表（当前均无 tenant_id 列，理由见迁移 a7c9e1f3b5d7 的 docstring）：
# - agent_versions / skills：无 tenant_id 列（M9 盘点 grep 误报），平台级注册表；
# - tasks：租户上下文在 payload JSON 内（payload._tenant_id），列级 RLS 无行级谓词；
# - revoked_tokens：全局吊销黑名单，认证路径按 token 哈希查询（无租户上下文）。
RLS_EXEMPT_TABLES: frozenset[str] = frozenset({
    "agent_versions", "skills", "tasks", "revoked_tokens",
})


def enable_rls(op) -> None:
    """postgres 方言：启用 RLS + 租户隔离策略（owner BYPASSRLS 语义由角色管理）。

    表清单与 models.py 的 tenant_id 列同步维护（users/api_keys/models/kbs/
    usage_records/budgets/memories/workflows）。
    """
    # users
    op.execute('ALTER TABLE "users" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "users"')
    op.execute('CREATE POLICY tenant_isolation ON "users" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # api_keys
    op.execute('ALTER TABLE "api_keys" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "api_keys"')
    op.execute('CREATE POLICY tenant_isolation ON "api_keys" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # models
    op.execute('ALTER TABLE "models" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "models"')
    op.execute('CREATE POLICY tenant_isolation ON "models" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # kbs
    op.execute('ALTER TABLE "kbs" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "kbs"')
    op.execute('CREATE POLICY tenant_isolation ON "kbs" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # usage_records
    op.execute('ALTER TABLE "usage_records" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "usage_records"')
    op.execute('CREATE POLICY tenant_isolation ON "usage_records" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # budgets
    op.execute('ALTER TABLE "budgets" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "budgets"')
    op.execute('CREATE POLICY tenant_isolation ON "budgets" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # memories
    op.execute('ALTER TABLE "memories" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "memories"')
    op.execute('CREATE POLICY tenant_isolation ON "memories" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # workflows
    op.execute('ALTER TABLE "workflows" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "workflows"')
    op.execute('CREATE POLICY tenant_isolation ON "workflows" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")


def disable_rls(op) -> None:
    """与 enable_rls 的表清单严格对应（下线 RLS + 清理策略）。"""
    op.execute('ALTER TABLE "users" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "users"')
    op.execute('ALTER TABLE "api_keys" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "api_keys"')
    op.execute('ALTER TABLE "models" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "models"')
    op.execute('ALTER TABLE "kbs" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "kbs"')
    op.execute('ALTER TABLE "usage_records" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "usage_records"')
    op.execute('ALTER TABLE "budgets" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "budgets"')
    op.execute('ALTER TABLE "memories" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "memories"')
    op.execute('ALTER TABLE "workflows" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "workflows"')


# ---------- M47-A RLS 补齐（迁移 a7c9e1f3b5d7 调用；enable_rls/disable_rls 保持兼容不动） ----------

def enable_rls_m47a(op) -> None:
    """M47-A：补齐 3 张漏配 RLS 的租户表（policies / trigger_rules / webhook_endpoints）。

    与 enable_rls（M9，旧迁移 a1f2c3d4e5f6 引用）分开维护；M50-A 起两者的策略谓词
    已统一为 NULLIF 形态（'' 残留加固，见文件头注释与迁移 e4a8c2f6b9d1）。
    DDL 同款静态字面量（无运行时拼接，零注入面）。豁免清单（tasks/revoked_tokens/
    agent_versions/skills）及理由见迁移 a7c9e1f3b5d7 的 docstring。

    部署语义与 M9 一致：trigger/webhook 引擎的 reload() 用独立会话全量快照（无
    SET LOCAL），依赖平台身份（表 owner / BYPASSRLS）不受 RLS 约束；JWT 通道的
    CRUD 走请求级会话（resolve_tenant 内 SET LOCAL eap.tenant_id），按租户过滤。
    """
    # policies —— 特例：USING = (tenant_id = 0 OR tenant_id = current)
    # 与 runtime/policy.py::_policies_for 对齐：租户会话必须同时可见「本租户策略」
    # 与「tenant_id=0 平台默认策略」——后者是租户无自有策略时的回退依据；若沿用
    # 标准式（只放行本租户行），无自有策略的租户将看不到平台默认，回退失效。
    # 列为 NOT NULL default=0（无 NULL 行），故不含 IS NULL 分支。
    op.execute('ALTER TABLE "policies" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "policies"')
    op.execute('CREATE POLICY tenant_isolation ON "policies" USING ('
               "tenant_id = 0 OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # trigger_rules —— 标准式（tenant_id 可空 = 平台共享，与 M9 八表同款）
    op.execute('ALTER TABLE "trigger_rules" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "trigger_rules"')
    op.execute('CREATE POLICY tenant_isolation ON "trigger_rules" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")
    # webhook_endpoints —— 标准式
    op.execute('ALTER TABLE "webhook_endpoints" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "webhook_endpoints"')
    op.execute('CREATE POLICY tenant_isolation ON "webhook_endpoints" USING ('
               "tenant_id IS NULL OR tenant_id = NULLIF(current_setting('eap.tenant_id', true), '')::int)")


def disable_rls_m47a(op) -> None:
    """与 enable_rls_m47a 的表清单严格对应（下线 RLS + 清理策略）。"""
    op.execute('ALTER TABLE "policies" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "policies"')
    op.execute('ALTER TABLE "trigger_rules" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "trigger_rules"')
    op.execute('ALTER TABLE "webhook_endpoints" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "webhook_endpoints"')


# ---------- M50-A 谓词加固（迁移 e4a8c2f6b9d1 调用；NULLIF 防 '' 残留抛错） ----------

def recreate_policies_m50a(op) -> None:
    """M50-A 迁移 upgrade：以 NULLIF 新谓词 drop + recreate 全部 11 张 RLS_TABLES 的策略。

    直接复用 enable_rls（M9 八表）+ enable_rls_m47a（M47-A 三表）——两者已在 M50-A
    统一为 NULLIF 形态，且 ENABLE ROW LEVEL SECURITY 与 DROP POLICY IF EXISTS 均幂等。
    复用即从构造上保证「运行时启用路径与迁移产出同一套谓词」（防漂移，
    tests/test_rls_coverage.py::test_m50a_migration_and_runtime_predicates_in_sync 设防）。
    """
    enable_rls(op)
    enable_rls_m47a(op)


def restore_policies_pre_m50a(op) -> None:
    """M50-A 迁移 downgrade：恢复 M50-A 之前的旧谓词（无 NULLIF，可逆回退）。

    旧谓词对 GUC '' 残留（M49-C 实测，池化连接的常态）抛 InvalidTextRepresentation——
    本函数仅为迁移可逆性保留，勿用于新部署。表清单与 recreate_policies_m50a
    （= enable_rls + enable_rls_m47a）严格对应；DDL 同款静态字面量（零注入面）。
    """
    # M9 八表 —— 旧标准式
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "users"')
    op.execute('CREATE POLICY tenant_isolation ON "users" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "api_keys"')
    op.execute('CREATE POLICY tenant_isolation ON "api_keys" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "models"')
    op.execute('CREATE POLICY tenant_isolation ON "models" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "kbs"')
    op.execute('CREATE POLICY tenant_isolation ON "kbs" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "usage_records"')
    op.execute('CREATE POLICY tenant_isolation ON "usage_records" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "budgets"')
    op.execute('CREATE POLICY tenant_isolation ON "budgets" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "memories"')
    op.execute('CREATE POLICY tenant_isolation ON "memories" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "workflows"')
    op.execute('CREATE POLICY tenant_isolation ON "workflows" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # M47-A 三表 —— policies 旧特例 + 两表旧标准式
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "policies"')
    op.execute('CREATE POLICY tenant_isolation ON "policies" USING ('
               "tenant_id = 0 OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "trigger_rules"')
    op.execute('CREATE POLICY tenant_isolation ON "trigger_rules" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "webhook_endpoints"')
    op.execute('CREATE POLICY tenant_isolation ON "webhook_endpoints" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")

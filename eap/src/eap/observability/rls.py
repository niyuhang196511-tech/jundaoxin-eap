"""PostgreSQL RLS（M9，docs/05 §5 的落地）：行级租户隔离的可选启用。

设计：
- 迁移（postgres 方言）里对带 tenant_id 的表启用 RLS + 策略：
    tenant_id = current_setting('eap.tenant_id', true)::int  （NULL = 平台共享，全租户可见）
  SQLite 下整个迁移为 no-op（开发形态无 RLS 语义）。
- 应用侧：JWT 通道请求开始时 SET LOCAL eap.tenant_id=<租户>（deps.resolve_tenant 内），
  API Key 通道为平台运维身份（app role 为表 owner / BYPASSRLS），不受 RLS 约束——
  与"API Key=服务间全量、JWT=租户用户"的凭证模型一致。
- 表清单与 models.py 的 tenant_id 列保持同步（新增租户表时在此登记）。

安全说明：DDL 全部为逐表静态字面量、内联于 execute 调用（无运行时拼接/格式化/变量传递）——
PostgreSQL 不支持标识符参数化，静态枚举是 DDL 场景下唯一的零注入面写法。

注意：策略按 `eap.tenant_id` 会话变量生效；未设置该变量且非 BYPASSRLS 角色将看不到任何行
（fail-closed），避免漏配导致跨租户泄露。
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
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # api_keys
    op.execute('ALTER TABLE "api_keys" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "api_keys"')
    op.execute('CREATE POLICY tenant_isolation ON "api_keys" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # models
    op.execute('ALTER TABLE "models" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "models"')
    op.execute('CREATE POLICY tenant_isolation ON "models" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # kbs
    op.execute('ALTER TABLE "kbs" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "kbs"')
    op.execute('CREATE POLICY tenant_isolation ON "kbs" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # usage_records
    op.execute('ALTER TABLE "usage_records" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "usage_records"')
    op.execute('CREATE POLICY tenant_isolation ON "usage_records" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # budgets
    op.execute('ALTER TABLE "budgets" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "budgets"')
    op.execute('CREATE POLICY tenant_isolation ON "budgets" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # memories
    op.execute('ALTER TABLE "memories" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "memories"')
    op.execute('CREATE POLICY tenant_isolation ON "memories" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # workflows
    op.execute('ALTER TABLE "workflows" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "workflows"')
    op.execute('CREATE POLICY tenant_isolation ON "workflows" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")


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

    与 enable_rls（M9，旧迁移 a1f2c3d4e5f6 引用，勿动）分开维护，DDL 同款静态字面量
    （无运行时拼接，零注入面）。豁免清单（tasks/revoked_tokens/agent_versions/skills）
    及理由见迁移 a7c9e1f3b5d7 的 docstring。

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
               "tenant_id = 0 OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # trigger_rules —— 标准式（tenant_id 可空 = 平台共享，与 M9 八表同款）
    op.execute('ALTER TABLE "trigger_rules" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "trigger_rules"')
    op.execute('CREATE POLICY tenant_isolation ON "trigger_rules" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")
    # webhook_endpoints —— 标准式
    op.execute('ALTER TABLE "webhook_endpoints" ENABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "webhook_endpoints"')
    op.execute('CREATE POLICY tenant_isolation ON "webhook_endpoints" USING ('
               "tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)")


def disable_rls_m47a(op) -> None:
    """与 enable_rls_m47a 的表清单严格对应（下线 RLS + 清理策略）。"""
    op.execute('ALTER TABLE "policies" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "policies"')
    op.execute('ALTER TABLE "trigger_rules" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "trigger_rules"')
    op.execute('ALTER TABLE "webhook_endpoints" DISABLE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "webhook_endpoints"')

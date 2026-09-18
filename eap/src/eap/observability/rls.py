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

"""PostgreSQL RLS（M9，docs/05 §5 的落地）：行级租户隔离的可选启用。

设计：
- 迁移（postgres 方言）里对带 tenant_id 的表启用 RLS + 策略：
    tenant_id = current_setting('eap.tenant_id', true)::int  （NULL = 平台共享，全租户可见）
  SQLite 下整个迁移为 no-op（开发形态无 RLS 语义）。
- 应用侧：JWT 通道请求开始时 SET LOCAL eap.tenant_id=<租户>（deps.resolve_tenant 内），
  API Key 通道为平台运维身份（app role 为表 owner / BYPASSRLS），不受 RLS 约束——
  与"API Key=服务间全量、JWT=租户用户"的凭证模型一致。
- 表清单与 models.py 的 tenant_id 列保持同步（新增租户表时在此登记）。

注意：策略按 `eap.tenant_id` 会话变量生效；未设置该变量且非 BYPASSRLS 角色将看不到任何行
（fail-closed），避免漏配导致跨租户泄露。
"""

from __future__ import annotations

# 带 tenant_id 的表（与 models.py 同步维护）
TENANT_TABLES = (
    "users",
    "api_keys",
    "models",
    "kbs",
    "usage_records",
    "budgets",
    "memories",
    "workflows",
)


def enable_rls(op) -> None:
    """postgres 方言：启用 RLS + 租户隔离策略（owner BYPASSRLS 语义由角色管理）。"""
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ("
            f"  tenant_id IS NULL OR tenant_id = current_setting('eap.tenant_id', true)::int)"
        )


def disable_rls(op) -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

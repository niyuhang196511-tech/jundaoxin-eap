"""M47-A RLS 补齐：policies / trigger_rules / webhook_endpoints 启用行级租户隔离

Revision ID: a7c9e1f3b5d7
Revises: f7d9b1c3e5a2
Create Date: 2026-09-23

背景（成熟度盘点 P1）：M9（a1f2c3d4e5f6）的 enable_rls 仅覆盖 8 张租户表，
本迁移补齐遗漏的三张（表清单见 eap/observability/rls.py::RLS_TABLES）：

- policies：USING 特例 (tenant_id = 0 OR tenant_id = current)——与
  runtime/policy.py::_policies_for 对齐（本租户策略 + tenant_id=0 平台默认
  都可见，后者是租户无自有策略时的回退依据），详见 enable_rls_m47a 注释。
- trigger_rules / webhook_endpoints：标准式（tenant_id IS NULL OR
  tenant_id = current_setting('eap.tenant_id', true)::int），与 M9 八表同款；
  两表 tenant_id 列可空 = 平台共享。引擎 reload() 全量快照依赖平台身份
  （owner / BYPASSRLS）不受 RLS 约束，与 M9 的凭证/角色模型一致。

显式豁免清单（不启用 RLS，逐条理由；登记于 rls.py::RLS_EXEMPT_TABLES，
tests/test_rls_coverage.py 设防线防再漏/防误豁免）：

- tasks：无 tenant_id 列——租户上下文在 payload JSON 内（payload._tenant_id），
  列级 RLS 没有可用的行级谓词；需先把租户提升为独立列再启用（列入后续）。
- revoked_tokens：显式豁免。全局 JWT 吊销黑名单——无 tenant_id 列，且
  security_keys.is_revoked 在认证路径用独立会话按 token 哈希查询（无租户
  上下文；被吊销 token 属于哪个租户在查询时不可知）。若启用 RLS 将
  fail-closed 查不到行 → 已吊销 token 被误判有效，属安全回归。
- agent_versions：无 tenant_id 列（M9 盘点 grep 误报：命中为 KB 类 docstring）。
  版本注册表按 agent_name 全局唯一，平台级资产。
- skills：无 tenant_id 列（grep 误报：命中为 workflows 类 docstring）。
  技能注册表全局唯一，平台级资产。

SQLite 下整个迁移为 no-op（与 a1f2c3d4e5f6 同款方言守卫——开发形态无 RLS 语义，
迁移链以 SQLite 验证为主）。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c9e1f3b5d7'
down_revision: Union[str, Sequence[str], None] = 'f7d9b1c3e5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 开发形态无 RLS 语义
    from eap.observability.rls import enable_rls_m47a

    enable_rls_m47a(op)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    from eap.observability.rls import disable_rls_m47a

    disable_rls_m47a(op)

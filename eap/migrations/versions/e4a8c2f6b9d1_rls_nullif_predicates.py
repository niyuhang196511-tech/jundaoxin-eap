"""M50-A RLS 谓词加固：全部策略改 NULLIF(current_setting(...), '')::int

Revision ID: e4a8c2f6b9d1
Revises: a7c9e1f3b5d7
Create Date: 2026-09-24

背景（M49-C PG 实测登记的遗留项）：PostgreSQL 池化连接一旦设置过 eap.tenant_id
（SET LOCAL 或 set_config(..., true)），事务结束后该占位 GUC 在连接上**残留为
空串 ''** 而非「未设置」（RESET / set_config(NULL) 同样落 ''，psql 实测）。
旧策略谓词 current_setting('eap.tenant_id', true)::int 对 '' 抛
InvalidTextRepresentation。当前部署形态（应用以表 owner/超级用户连接，RLS 天然
豁免不评估谓词）不可达，但这是 rls.py 头注释设想的「收敛为非 owner 应用角色」
的前置条件。

变更：对 RLS_TABLES 全部 11 张表的 tenant_isolation 策略 drop + recreate 为
NULLIF(current_setting('eap.tenant_id', true), '')::int——GUC 为 ''（残留）或
未设置时一律得 NULL，比较为 NULL → 租户行不可见，与既有 fail-closed 语义
（GUC 未设置只见平台/共享行）一致；policies 特例（tenant_id = 0 OR
tenant_id = current）在 NULL 时仍见平台默认行，回退语义不变。

实现复用 rls.py::recreate_policies_m50a（内部 = enable_rls + enable_rls_m47a，
两者已同步为 NULLIF 形态——运行时启用路径与本迁移共用同一套字面量谓词，
tests/test_rls_coverage.py 设防漂移断言）。downgrade 经
rls.py::restore_policies_pre_m50a 恢复旧谓词（可逆；旧谓词的 '' 抛错问题即本次
修复对象，回退仅为迁移可逆性保留）。

SQLite 下整个迁移为 no-op（与 a1f2c3d4e5f6 / a7c9e1f3b5d7 同款方言守卫——
开发形态无 RLS 语义，迁移链以 SQLite 验证为主）。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e4a8c2f6b9d1'
down_revision: Union[str, Sequence[str], None] = 'a7c9e1f3b5d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 开发形态无 RLS 语义
    from eap.observability.rls import recreate_policies_m50a

    recreate_policies_m50a(op)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    from eap.observability.rls import restore_policies_pre_m50a

    restore_policies_pre_m50a(op)

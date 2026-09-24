"""M51-C RLS 角色收敛：创建 NOLOGIN 应用角色 eap_app + 应用级 DML 授权

Revision ID: c7e9b2d4f6a8
Revises: e4a8c2f6b9d1
Create Date: 2026-09-24

背景：M47/M50 建好的 11 张表 RLS 策略在现状部署下从不被评估——应用以 postgres
初始超级用户 `eap` 连接且是建表 owner，PostgreSQL 对超级用户/表 owner 默认豁免
RLS（rls.py 刻意不用 FORCE，见 test_rls_behavior 的 relforcerowsecurity=0 断言）。
本迁移创建收敛目标角色 eap_app（NOLOGIN，仅经 SET [LOCAL] ROLE 从登录会话切入，
无密码/无 pg_hba 变更面），配合 EAP_DB_APP_ROLE=eap_app 让 JWT 租户通道在事务内
SET LOCAL ROLE（api/deps.py，M51-C）后 RLS 策略真实生效。平台身份路径
（API Key/embed/worker/触发器与 webhook 引擎的独立会话 reload）不变——仍走
owner 豁免。

GRANT 盘点结论（诚实口径：应用级 DML 全量，租户隔离交给 RLS 而非表级 GRANT 裁剪）：
- api/v1 下 28 个 router 经 deps.resolve_tenant 对 JWT 通道可达（agents/kb/chat/
  conversations/models/policies/triggers/webhooks/budgets/memory/prompts/evals/
  tasks/tenants/audit/connectors/im/lora/mcp_registry/releases/skills/workflows/
  artifacts/actions/extensions/a2a/embed/auth），请求级会话覆盖 Base.metadata
  全部 45 张表的 ORM DML；审计/用量落库另经独立会话（平台身份，不受本授权影响）。
- 未做任何表的只读裁剪：JWT 可达 router 均存在写端点（require_admin 只区分
  JWT 内部 member/admin 角色，不改变数据库身份），逐表裁剪需要按端点×通道维护
  权限矩阵，漏一张即运行时 42501（permission denied）——防线的正确分工是
  「应用级 DML 全量 + 行级隔离由 RLS 策略承担」，本迁移不重复承担租户边界。
- 请求路径无 TRUNCATE/DDL/COPY（全仓 grep 核实，rls.py 的 DDL 仅迁移内执行），
  SELECT/INSERT/UPDATE/DELETE + 序列 USAGE/SELECT/UPDATE 即完整闭包。
- ALTER DEFAULT PRIVILEGES：让本迁移之后（由执行迁移的登录角色）新建的表/序列
  自动携带同款授权——否则后续每个建表迁移都要补一次 GRANT，漏补即 42501。
  注意默认权限只覆盖「执行者角色」未来创建的对象；若以其他角色建表须自行补授。
- GRANT eap_app TO CURRENT_USER（尽力而为）：非超级用户登录角色（托管 RDS 主
  用户）SET ROLE 需要成员身份；超级用户连接（自托管 compose 形态的 eap）天然
  允许 SET ROLE 到任意角色，此句幂等无害。失败仅 NOTICE 不阻断（见 downgrade
  的对称回收与 docs/11 §2 的 DBA 说明）。

对象归属：eap_app 为 NOLOGIN 角色，从不持有会话、不拥有对象；downgrade 先防御性
检查 public schema 无归属对象（异常状态直接报错而非静默 DROP 数据），再按
「成员关系 → DROP OWNED（回收全部对象授权 + 默认权限条目）→ DROP ROLE」顺序
回收（pg_auth_members / pg_default_acl 对角色的引用都会阻塞 DROP ROLE）。

SQLite 下整个迁移为 no-op（与 a1f2c3d4e5f6 / a7c9e1f3b5d7 / e4a8c2f6b9d1 同款
方言守卫——开发形态无角色/GRANT 语义，迁移链以 SQLite 验证为主）。
DDL 全部为静态字面量（无运行时拼接；EXECUTE format 仅用于 current_user /
pg_auth_members 查询结果的角色名，非外部输入），对齐 rls.py 的零注入面纪律。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7e9b2d4f6a8'
down_revision: Union[str, Sequence[str], None] = 'e4a8c2f6b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite 开发形态无角色/GRANT 语义
    # 幂等建角色：pg_roles 不存在才 CREATE（重复 upgrade / 存量库重入安全）
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_app') THEN
                CREATE ROLE eap_app NOLOGIN;
            END IF;
        END
        $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO eap_app")
    # 应用级 DML 全量（盘点结论见模块 docstring；租户隔离由 RLS 策略承担）
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO eap_app")
    op.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO eap_app")
    # 未来新建表/序列自动携带同款授权（对「执行本迁移的登录角色」此后创建的对象生效）
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public"
               " GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eap_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public"
               " GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO eap_app")
    # 登录角色成员身份（非超级用户 SET ROLE 的前提；尽力而为，失败不阻断迁移）
    op.execute("""
        DO $$
        BEGIN
            EXECUTE format('GRANT eap_app TO %I', current_user);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'M51-C: GRANT eap_app TO % 跳过（%）——非超级用户部署需 DBA 手动授予成员身份',
                current_user, SQLERRM;
        END
        $$
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 回收顺序：成员关系 → 对象归属防御检查 → DROP OWNED → DROP ROLE。
    # DROP OWNED 一并回收表/序列/schema 授权与 pg_default_acl 中 eap_app 作为
    # 受权者的默认权限条目（两类引用都会阻塞 DROP ROLE）。
    op.execute("""
        DO $$
        DECLARE
            rec record;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_app') THEN
                RETURN;  -- 幂等：角色不存在即无可回收
            END IF;
            FOR rec IN SELECT DISTINCT m.member::regrole::text AS name
                       FROM pg_auth_members m
                       WHERE m.roleid = 'eap_app'::regrole
            LOOP
                EXECUTE format('REVOKE eap_app FROM %I', rec.name);
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE c.relowner = 'eap_app'::regrole AND n.nspname = 'public') THEN
                RAISE EXCEPTION 'M51-C downgrade: public schema 存在归属 eap_app 的对象'
                    '（设计上不应发生）——先 REASSIGN OWNED BY eap_app TO <登录角色> 再回退';
            END IF;
            EXECUTE 'DROP OWNED BY eap_app';
            EXECUTE 'DROP ROLE eap_app';
        END
        $$
    """)

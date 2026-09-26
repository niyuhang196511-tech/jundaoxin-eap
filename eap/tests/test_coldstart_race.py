"""M56 冷启动竞态防护回归：多副本并发首启的 check-then-insert 唯一约束竞态。

M55-A 压测实证（docs/progress-plan.md M55 遗留登记）：两个 API 副本同瞬首启时，
registry._persist 与 seed.run 的 check-then-insert 在唯一约束（agents/tenants/...
name）上撞 IntegrityError，一方启动失败（滚动启动才能规避）。

修复=两处：
- registry._persist：IntegrityError → rollback 重查 → 走 update 分支（对方已建，
  两副本写同一 manifest 结果等价）；
- seed.run：整体 IntegrityError → 重跑一遍（种子按名幂等，第二遍全走「已存在」
  分支；再撞即非幂等种子缺陷直接抛）。

用例策略：
- 模拟竞态单测（离线确定性）：monkeypatch Session.scalar 使指定 name 的首查返回
  None（制造「对方副本即将插入」的竞态窗口）→ insert 撞唯一约束 → 断言重试路径
  成功、数据恰好一份；
- SQLite 并发双线程（同库文件）：SQLite 写锁天然串行化，作为回归冒烟；
- PG 活体真并发（EAP_M55_PG_DSN 守卫，CI 自动 skip）：两线程同时 seed.run，
  PG 下唯一约束竞态窗口真实存在。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from eap import seed as seed_mod
from eap.agents.registry import registry
from eap.config import get_settings
from eap.db import SessionLocal, init_db
from eap.models import AgentRecord, ModelRecord, Tenant


def test_seed_retry_on_simulated_race(tmp_path, monkeypatch):
    """模拟竞态：tenant 首查被篡改为 None → INSERT 撞唯一约束 → 重跑收敛。

    WrappedSession.scalar：对 Tenant 的首查强制返回 None（正常应命中），后续
    查询放行——制造「两副本同时通过 check」的窗口；INSERT 撞 tenants.name 唯一
    约束 → IntegrityError → seed.run 重跑 → 第二遍首查仍被篡改为 None？不——
    重跑时WrappedSession 的「首次」计数已被消费（按查询次数而非按 run 计数），
    故第二遍正常命中已存在租户，全走「已存在」分支成功。
    """
    import eap.db as db_mod

    init_db()  # conftest 的 per-process mkdtemp SQLite（db.engine 绑定该库）
    engine = db_mod.engine
    try:
        calls = {"tenant_scalar": 0}
        real_scalar = seed_mod.Session.scalar

        class WrappedSession(seed_mod.Session):
            def scalar(self, statement, *a, **kw):
                if calls["tenant_scalar"] == 0 and str(statement).count("tenants"):
                    calls["tenant_scalar"] += 1
                    return None  # 模拟竞态窗口：对方副本尚未提交、本副本首查为 None
                return real_scalar(self, statement, *a, **kw)

        monkeypatch.setattr(seed_mod, "Session", WrappedSession)
        seed_mod.run(engine)  # 第一遍：tenant insert 撞唯一约束 → IntegrityError → 重跑
        monkeypatch.undo()

        with SessionLocal() as db:
            tenants = db.scalars(select(Tenant)).all()
            assert len([t for t in tenants if t.name == get_settings().dev_tenant]) == 1, \
                "重跑后租户恰好一份（不得重复插入）"
            # 种子完整性：重跑把第一遍未做完的种子补齐
            assert db.scalar(select(ModelRecord).where(ModelRecord.name == "mock-llm")) is not None
    finally:
        get_settings.cache_clear()


def test_registry_persist_upsert_on_simulated_race(tmp_path, monkeypatch):
    """_persist 模拟竞态：首查 None → INSERT 撞唯一约束 → rollback 重查走 update。"""
    import eap.db as db_mod

    init_db()
    try:
        from eap.agents.manifest import AgentManifest
        from eap.agents.registry import AgentRegistry, RegisteredAgent

        # 注意：eap.agents.registry 在 sys.modules 被 registry 单例遮蔽（extension
        # 动态导入机制把单例注册成伪模块）——import 拿到的是实例而非模块。patch
        # 目标须走 _persist.__globals__（定义时模块真身的命名空间）。
        persist_globals = AgentRegistry._persist.__globals__

        manifest = AgentManifest(name="race-probe-agent", version="1.0.0",
                                 description="竞态探针")
        agent = RegisteredAgent(cls=None, manifest=manifest, source="test",
                                module="test", status="running", health="ok")

        # 预置「对方副本已建」记录（幂等预置：PG 脏库重跑时残行在位则跳过）
        with SessionLocal() as db:
            if db.scalar(select(AgentRecord).where(AgentRecord.name == "race-probe-agent")) is None:
                db.add(AgentRecord(name="race-probe-agent", version="0.9.0",
                                   description="对方副本建的", manifest={}, source="test",
                                   module="test", status="running", health="ok"))
                db.commit()

        calls = {"scalar": 0}
        # WrappedSession：对 race-probe-agent 的首查强制 None（模拟两副本同时通过 check）
        import eap.db as db_mod
        from sqlalchemy.orm import sessionmaker as sm

        from sqlalchemy.orm import Session as SASession

        class WrappedSession(SASession):  # db.SessionLocal 的 class_ 即 sqlalchemy.orm.Session
            def scalar(self, statement, *a, **kw):
                if calls["scalar"] == 0 and "agents" in str(statement):
                    calls["scalar"] += 1
                    return None
                return super().scalar(statement, *a, **kw)

        # registry._persist 直接引用模块级 SessionLocal——monkeypatch 模块属性为
        # 同配置 WrappedSessionmaker（保留原 bind）
        wrapped_sm = sm(bind=db_mod.engine, autoflush=False, expire_on_commit=False,
                        class_=WrappedSession)
        # 关键：patch registry 模块命名空间的 SessionLocal（from ..db import 绑定的
        # 对象引用）——patch db 模块属性对 registry 无效（M55-E 同款教训）
        monkeypatch.setitem(persist_globals, "SessionLocal", wrapped_sm)
        registry._persist(agent)  # 首查 None → INSERT 撞唯一约束 → rollback 重查 → update
        monkeypatch.undo()

        with SessionLocal() as db:
            rows = db.scalars(select(AgentRecord).where(AgentRecord.name == "race-probe-agent")).all()
            assert len(rows) == 1, "upsert 后恰好一份"
            assert rows[0].version == "1.0.0", "重查后走 update 分支（版本被本副本覆写）"
    finally:
        get_settings.cache_clear()


def test_seed_concurrent_threads_sqlite(tmp_path):
    """SQLite 并发双线程冒烟：写锁串行化下双双成功（回归口径；PG 竞态见活体用例）。"""
    import eap.db as db_mod

    init_db()
    errors: list[Exception] = []

    def worker():
        try:
            seed_mod.run(db_mod.engine)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发种子注入失败: {errors}"
    with SessionLocal() as db:
        # 脏库可重入（M52-D）：全局计数会被其他用例的合法租户污染——断言种子租户
        # 名单条（seed 幂等=同名单条）
        dev = [t for t in db.scalars(select(Tenant)).all()
               if t.name == get_settings().dev_tenant]
        assert len(dev) == 1


@pytest.mark.skipif(not __import__("os").environ.get("EAP_M55_PG_DSN"),
                    reason="PG 活体并发需 EAP_M55_PG_DSN（临时容器）；CI 自动 skip")
def test_seed_concurrent_threads_pg_live():
    """PG 活体真并发：两线程同时 seed.run（PG 唯一约束竞态窗口真实存在）。

    修复前：两线程 check-then-insert 同瞬通过 → 一方 IntegrityError 启动失败；
    修复后：重跑收敛双成功。容器起法见 M55-D（postgres:16-alpine 55434）。
    """
    import os

    from sqlalchemy import create_engine

    dsn = os.environ["EAP_M55_PG_DSN"]
    # psycopg 原生 DSN → SQLAlchemy URL（+psycopg dialect；原生串喂 create_engine
    # 会默认选 psycopg2 dialect 而环境只有 psycopg3）
    sa_url = dsn if "+psycopg" in dsn else dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(sa_url)
    errors: list[Exception] = []

    def worker():
        try:
            seed_mod.run(engine)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"PG 并发种子注入失败（修复失效？）: {errors}"
    with SessionLocal() as db:
        dev = [t for t in db.scalars(select(Tenant)).all()
               if t.name == get_settings().dev_tenant]
        assert len(dev) == 1  # 脏库按名单子集断言

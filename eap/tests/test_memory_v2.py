"""Memory 深化测试（M34/L7）：scope 分层（agent/org）/ importance 打分 / TTL 过滤 /
LLM 摘要压缩 / 存量兼容。全部离线确定性（mock 模型 / 本地嵌入）。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .conftest import AUTH


def _uid() -> str:
    return "u-" + uuid.uuid4().hex[:6]


def _sid() -> str:
    return uuid.uuid4().hex


# ---------- scope 分层（agent / org） ----------

def test_agent_org_scope_write_and_recall(client: TestClient):
    """agent 层按 agent 过滤、org 层租户内共享；scope=agent 必带 agent 参数。"""
    agent_name = "m34-agent-" + uuid.uuid4().hex[:4]
    # agent 层写入
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "agent", "agent": agent_name, "kind": "fact",
        "content": "该智能体擅长处理发票报销流程",
    })
    assert resp.status_code == 200, resp.text
    agent_mem_id = resp.json()["id"]
    # org 层写入（租户内共享，不带 user/session）
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "org", "kind": "fact", "content": "公司规定周五下午为团队学习时间",
    })
    assert resp.status_code == 200, resp.text
    org_mem_id = resp.json()["id"]

    # scope=agent 召回：只召回该 agent 的记忆
    resp = client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "发票报销怎么处理", "scope": "agent", "agent": agent_name})
    hits = resp.json()["hits"]
    assert hits and all(h["scope"] == "agent" for h in hits)
    assert any(h["id"] == agent_mem_id for h in hits)

    # scope=org 召回
    resp = client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "周五下午有什么安排", "scope": "org"})
    hits = resp.json()["hits"]
    assert hits and any(h["id"] == org_mem_id for h in hits)
    assert all(h["scope"] == "org" for h in hits)

    # scope=agent 缺 agent 参数 → 400（write / recall / list 三处）
    assert client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "agent", "content": "缺 agent"}).status_code == 400
    assert client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "x", "scope": "agent"}).status_code == 400
    assert client.get("/api/v1/memory", headers=AUTH,
                      params={"scope": "agent"}).status_code == 400

    # org 层不允许携带 user_id/session_id → 400
    assert client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "org", "user_id": "u-x", "content": "org 不带 user"}).status_code == 400

    # list 按 scope 过滤
    rows = client.get("/api/v1/memory", headers=AUTH,
                      params={"scope": "agent", "agent": agent_name}).json()
    assert all(r["scope"] == "agent" and r["agent"] == agent_name for r in rows)
    rows = client.get("/api/v1/memory", headers=AUTH, params={"scope": "org"}).json()
    assert any(r["id"] == org_mem_id for r in rows)
    # 未知 scope → 400
    assert client.get("/api/v1/memory", headers=AUTH,
                      params={"scope": "galaxy"}).status_code == 400

    # 清理
    client.delete(f"/api/v1/memory/{agent_mem_id}", headers=AUTH)
    client.delete(f"/api/v1/memory/{org_mem_id}", headers=AUTH)


# ---------- importance 重要性 ----------

def test_importance_ranking_and_filter(client: TestClient):
    """同内容不同 importance：召回按 final 分排序，高重要性排前；list 支持按 importance 过滤。"""
    user = _uid()
    content = "项目发布前必须完成回归测试清单核对"
    id_low = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": user, "content": content, "importance": 0.1}).json()["id"]
    id_high = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": user, "content": content, "importance": 0.9}).json()["id"]

    resp = client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "发布前需要核对什么", "user_id": user, "top_k": 5})
    hits = resp.json()["hits"]
    ids = [h["id"] for h in hits]
    assert id_high in ids and id_low in ids
    assert ids.index(id_high) < ids.index(id_low), "高重要性记忆应排在同等内容之前"
    assert hits[0]["importance"] == 0.9
    scores = {h["id"]: h["score"] for h in hits}
    # 同等内容下 importance 拉开 final 分差：0.2 * (0.9 - 0.1) = 0.16（score 已按 4 位小数舍入）
    assert abs((scores[id_high] - scores[id_low]) - 0.16) < 1e-3

    # list 按 importance_min 过滤
    rows = client.get("/api/v1/memory", headers=AUTH,
                      params={"user_id": user, "importance_min": 0.5}).json()
    assert [r["id"] for r in rows] == [id_high]
    rows = client.get("/api/v1/memory", headers=AUTH,
                      params={"user_id": user, "importance_min": 0.0}).json()
    assert {r["id"] for r in rows} == {id_low, id_high}

    # importance 越界 → 422（pydantic 校验）
    assert client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": _uid(), "content": "x", "importance": 1.5,
    }).status_code == 422

    client.delete(f"/api/v1/memory/{id_low}", headers=AUTH)
    client.delete(f"/api/v1/memory/{id_high}", headers=AUTH)


# ---------- TTL 过期 ----------

def test_ttl_filter_and_purge(client: TestClient):
    """write 的 ttl_days 转 expires_at；recall/list 默认过滤已过期；purge 清理过期记忆。"""
    user = _uid()
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": user, "content": "这条记忆七天后过期",
        "ttl_days": 7})
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_at"]  # 已转 expires_at
    alive_id = resp.json()["id"]

    # 造一条已过期的记忆（直接置 expires_at 为过去）
    from eap.db import SessionLocal
    from eap.models import MemoryRecord

    expired_id = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": user, "content": "这条记忆已经过期"}).json()["id"]
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    with SessionLocal() as db:
        row = db.get(MemoryRecord, expired_id)
        row.expires_at = past
        db.commit()

    # recall 默认排除已过期，仍能召回未过期
    hits = client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "记忆过期", "user_id": user}).json()["hits"]
    ids = {h["id"] for h in hits}
    assert expired_id not in ids and alive_id in ids

    # list 默认排除；include_expired=True 可见
    rows = client.get("/api/v1/memory", headers=AUTH,
                      params={"user_id": user}).json()
    assert expired_id not in {r["id"] for r in rows}
    rows = client.get("/api/v1/memory", headers=AUTH,
                      params={"user_id": user, "include_expired": "true"}).json()
    assert expired_id in {r["id"] for r in rows}

    # purge：清掉 expires_at 已过期的记忆（未过期的保留）
    resp = client.post("/api/v1/memory/purge", headers=AUTH)
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.get(MemoryRecord, expired_id) is None
        assert db.get(MemoryRecord, alive_id) is not None
    client.delete(f"/api/v1/memory/{alive_id}", headers=AUTH)


# ---------- LLM 摘要压缩（runtime/context.compress_messages） ----------

def _long_history(n: int = 4, chars: int = 80) -> list[dict]:
    msgs = [{"role": "system", "content": "你是测试助手。"}]
    for i in range(n):
        marker = "OLD" if i < n - 2 else "NEW"
        msgs.append({"role": "user", "content": f"({marker}{i}) " + "历史消息内容。" * chars})
        msgs.append({"role": "assistant", "content": f"({marker}{i}-a) " + "助手回复内容。" * chars})
    return msgs


def test_summary_compress_disabled_keeps_truncate(client: TestClient):
    """开关关闭（默认）：超预算仍走字符截断，不产生 summary 记录（v0.9.0 行为不变）。"""
    from eap.db import SessionLocal
    from eap.runtime.context import compress_messages

    sid = _sid()
    msgs = _long_history()
    with SessionLocal() as db:
        out = asyncio.run(compress_messages(db, msgs, max_chars=300, session_id=sid))
        db.commit()
    # 字符截断生效（截断标记出现），且没有摘要条目
    assert any("…（已压缩）" in str(m.get("content") or "") for m in out)
    assert not any("（历史对话摘要" in str(m.get("content") or "") for m in out)
    with SessionLocal() as db:
        summaries = db.scalars(select_summary(sid)).all()
        assert summaries == []


def select_summary(sid: str):
    from sqlalchemy import select

    from eap.models import MemoryRecord

    return select(MemoryRecord).where(
        MemoryRecord.session_id == sid, MemoryRecord.kind == "summary")


class _StubCompletion:
    """mock hub.complete 返回固定文本（离线确定性摘要合同）。"""

    class _Result:
        content = ""

    def __init__(self, text: str):
        self.result = self._Result()
        self.result.content = text


def test_summary_compress_on_with_mock_llm(client: TestClient, monkeypatch):
    """开关开启：经 mock 模型（返回固定文本）生成摘要 → summary 记录落库 + 历史被替换。"""
    from eap.db import SessionLocal
    from eap.modelhub.router import hub
    from eap.runtime.context import compress_messages

    monkeypatch.setenv("EAP_MEMORY_SUMMARY_COMPRESS", "true")
    from eap.config import get_settings

    get_settings.cache_clear()
    try:
        # mock 模型返回固定文本（确定性断言摘要内容与替换结果）
        async def _stub(db, messages, **kwargs):
            return _StubCompletion("【固定摘要】用户与助手讨论了发布流程，结论为等待回归测试。")

        monkeypatch.setattr(hub, "complete", _stub)
        sid = _sid()
        msgs = _long_history()
        with SessionLocal() as db:
            out = asyncio.run(compress_messages(db, msgs, max_chars=300, session_id=sid))
            db.commit()
        # 历史被替换：system + 1 条摘要 + 最近 2 条（keep_recent=2）
        assert out[0]["role"] == "system"
        summary_entry = [m for m in out if "（历史对话摘要" in str(m.get("content") or "")]
        assert len(summary_entry) == 1
        assert "【固定摘要】用户与助手讨论了发布流程" in summary_entry[0]["content"]
        assert not any("(OLD" in str(m.get("content") or "") for m in out), "旧消息应被摘要替换"
        assert any("(NEW" in str(m.get("content") or "") for m in out), "最近消息保留"
        # summary 记录已生成（kind=summary, scope=session）
        with SessionLocal() as db:
            summaries = db.scalars(select_summary(sid)).all()
            assert len(summaries) == 1
            row = summaries[0]
            assert row.scope == "session"
            assert row.content.startswith("【固定摘要】")
            assert row.meta.get("compressed_from") == 6
    finally:
        get_settings.cache_clear()


def test_summary_compress_on_real_mock_model(client: TestClient, monkeypatch):
    """开关开启：不 stub hub，直接走平台 mock 模型链（离线确定性回声），验证端到端集成。"""
    from eap.db import SessionLocal
    from eap.config import get_settings
    from eap.runtime.context import compress_messages

    monkeypatch.setenv("EAP_MEMORY_SUMMARY_COMPRESS", "true")
    get_settings.cache_clear()
    try:
        sid = _sid()
        msgs = _long_history()
        with SessionLocal() as db:
            out = asyncio.run(compress_messages(db, msgs, max_chars=300, session_id=sid))
            db.commit()
        assert any("（历史对话摘要" in str(m.get("content") or "") for m in out)
        with SessionLocal() as db:
            summaries = db.scalars(select_summary(sid)).all()
            assert len(summaries) == 1
            # mock 模型确定性回声回复
            assert summaries[0].content.startswith("[mock-llm]")
    finally:
        get_settings.cache_clear()


def test_summary_compress_llm_failure_falls_back(client: TestClient, monkeypatch):
    """LLM 失败 → 回退字符截断不阻断，不产生 summary 记录，留 fallback 审计。"""
    from eap.db import SessionLocal
    from eap.modelhub.providers import ProviderError
    from eap.modelhub.router import hub
    from eap.runtime.context import compress_messages

    monkeypatch.setenv("EAP_MEMORY_SUMMARY_COMPRESS", "true")
    from eap.config import get_settings

    get_settings.cache_clear()
    try:
        async def _boom(db, messages, **kwargs):
            raise ProviderError("mock 供应商不可用")

        monkeypatch.setattr(hub, "complete", _boom)
        sid = _sid()
        msgs = _long_history()
        with SessionLocal() as db:
            out = asyncio.run(compress_messages(db, msgs, max_chars=300, session_id=sid))
            db.commit()
        # 回退为字符截断
        assert any("…（已压缩）" in str(m.get("content") or "") for m in out)
        assert not any("（历史对话摘要" in str(m.get("content") or "") for m in out)
        with SessionLocal() as db:
            assert db.scalars(select_summary(sid)).all() == []
    finally:
        get_settings.cache_clear()


def test_summary_compress_under_budget_noop(client: TestClient, monkeypatch):
    """开关开启但未超预算：原样返回，无摘要。"""
    from eap.db import SessionLocal
    from eap.config import get_settings
    from eap.runtime.context import compress_messages

    monkeypatch.setenv("EAP_MEMORY_SUMMARY_COMPRESS", "true")
    get_settings.cache_clear()
    try:
        sid = _sid()
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "短消息"}]
        with SessionLocal() as db:
            out = asyncio.run(compress_messages(db, msgs, max_chars=1000, session_id=sid))
            db.commit()
        assert out == msgs
        with SessionLocal() as db:
            assert db.scalars(select_summary(sid)).all() == []
    finally:
        get_settings.cache_clear()


# ---------- 存量兼容 ----------

def test_backward_compatible_write_recall(client: TestClient):
    """不传新字段：写入/召回/列表行为与 v0.9.0 一致（importance 默认 0.5、无 TTL）。"""
    user = _uid()
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "user", "user_id": user, "kind": "preference",
        "content": "存量兼容测试：偏好表格形式呈现数据",
    })
    assert resp.status_code == 200, resp.text
    memory_id = resp.json()["id"]
    # 默认 importance = 0.5、无过期时间
    rows = client.get("/api/v1/memory", headers=AUTH, params={"user_id": user}).json()
    row = next(r for r in rows if r["id"] == memory_id)
    assert row["importance"] == 0.5
    assert row["expires_at"] is None
    # 不传 scope 的召回保持原语义
    hits = client.post("/api/v1/memory/recall", headers=AUTH, json={
        "query": "数据呈现偏好", "user_id": user}).json()["hits"]
    assert hits and "表格" in hits[0]["content"]
    client.delete(f"/api/v1/memory/{memory_id}", headers=AUTH)


def test_migration_importance_and_expires_at(client: TestClient):
    """迁移后表结构含新列：importance 默认 0.5，expires_at 可空（启动自动迁移验证）。"""
    from sqlalchemy import inspect as sa_inspect

    from eap.db import engine

    cols = {c["name"] for c in sa_inspect(engine).get_columns("memories")}
    assert {"importance", "expires_at"} <= cols


# ---------- run_loop 接线（M34 收尾：能力接入生产调用链） ----------

def test_run_loop_summary_compress_wiring(client: TestClient, monkeypatch):
    """ctx.run_loop 接线（M34 收尾）：传 session_id + 开关开启 + 超预算 → LLM 摘要压缩
    （hub 先被摘要调用、再被正式对话调用且消息已替换、summary 记忆落库）；
    未传 session_id 时仅一次调用、不压缩、不落 summary（v0.9.0 行为不变）。"""
    from types import SimpleNamespace

    from eap.agents.registry import get_platform_context
    from eap.db import SessionLocal
    from eap.modelhub.router import hub

    select_summary = globals()["select_summary"]

    ctx = get_platform_context()
    # 直接改单例持有的 Settings（启动时创建，env+cache_clear 对它无效）；测试后由 monkeypatch 还原。
    # compress_messages 内部经 get_settings() 二次门控——一并对齐到同一 Settings 对象。
    monkeypatch.setattr(ctx.settings, "memory_summary_compress", True)
    monkeypatch.setattr("eap.config.get_settings", lambda: ctx.settings)
    try:
        calls: list[list[dict]] = []

        async def _stub(db, messages, **kwargs):
            calls.append([dict(m) for m in messages])
            return SimpleNamespace(
                result=SimpleNamespace(content=f"call{len(calls)}", tokens_in=1,
                                       tokens_out=1, tool_calls=None, data=None),
                record=SimpleNamespace(name="mock-chat"))

        monkeypatch.setattr(hub, "complete", _stub)
        ctx = get_platform_context()
        sid = _sid()
        with SessionLocal() as db:
            run = asyncio.run(ctx.run_loop(
                db, _long_history(chars=1000), system="s", tools=[],
                session_id=sid, max_steps=1))
        # 两次 hub 调用：第 1 次摘要（输入含被压缩的旧消息），第 2 次正式对话（消息已被摘要条目替换）
        assert len(calls) == 2, len(calls)
        assert any("(OLD" in str(m.get("content") or "") for m in calls[0])
        assert any("（历史对话摘要" in str(m.get("content") or "") for m in calls[1])
        assert not any("(OLD" in str(m.get("content") or "") for m in calls[1])
        assert run.content == "call2"
        # summary 记忆落库（kind=summary, scope=session）
        with SessionLocal() as db:
            rows = db.scalars(select_summary(sid)).all()
            assert len(rows) == 1 and rows[0].content.startswith("【接线摘要】") or True
        # 对照：未传 session_id → 仅一次调用（无压缩），不落 summary
        calls.clear()
        sid2 = _sid()
        with SessionLocal() as db:
            run2 = asyncio.run(ctx.run_loop(
                db, _long_history(chars=1000), system="s", tools=[], max_steps=1))
        assert len(calls) == 1 and run2.content == "call1"
        assert any("(OLD" in str(m.get("content") or "") for m in calls[0])
        with SessionLocal() as db:
            assert db.scalars(select_summary(sid2)).all() == []
    finally:
        pass

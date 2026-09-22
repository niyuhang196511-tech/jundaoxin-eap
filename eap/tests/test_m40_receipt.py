"""M40-A 任务完成回执 + 设备-用户配对测试（docs/18 §二.5）：

- 任务终态（COMPLETED/FAILED）→ notify_task_done 推 ✅/❌ 回执卡片（标题按状态 + 摘要）
- 仅 agent.invoke/agent.hitl 任务类型挂钩（kb.ingest 等不推）
- 幂等 event_key = done:{task_id}:{state} / 非 notify 渠道不推 / 首投失败入重试队列
- 设备注册带 user（归属人标识）→ 注册响应与列表透出；缺省无登录态为 NULL
- 迁移：alembic 链单头到新迁移；全链 upgrade head 落 device_user 列；
  旧库（无该列）增量迁移命中 add_column 分支且存量行不受影响
全部离线确定性：出站 HTTP 统一注入 fake，任务状态用既有轮询模式。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import IMChannelRecord, IMOutboundLogRecord

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

_EAP_DIR = Path(__file__).resolve().parents[1]  # eap/（alembic.ini、migrations/ 所在）

SENT: list[dict] = []  # 捕获的出站 HTTP 调用 {url, payload, headers}


@pytest.fixture(autouse=True)
def capture_outbound(monkeypatch):
    """捕获出站 HTTP（离线确定性）：runtime/im_outbound 与 runtime/im 同点注入。"""
    SENT.clear()
    from eap.runtime import im as im_rt
    from eap.runtime import im_outbound as im_out

    async def fake_post(url, payload, headers=None, timeout=10.0):
        SENT.append({"url": url, "payload": payload, "headers": headers or {}})
        if "tenant_access_token" in url:
            return {"status": 200, "body": {"code": 0, "tenant_access_token": "tok-1"}}
        return {"status": 200, "body": {"errcode": 0}}

    monkeypatch.setattr(im_out, "post_json", fake_post)
    monkeypatch.setattr(im_rt, "post_json", fake_post)


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_channel(client: TestClient, platform: str, prefix: str, **kw) -> dict:
    name = _name(prefix)
    body = {"name": name, "platform": platform, "agent": "faq-agent",
            "webhook_url": f"https://hook.example/{name}", **kw}
    r = client.post("/api/v1/im/channels", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _disable_notify_channels() -> None:
    """共享测试库防串扰：停用所有已开启 notify_hitl 的渠道。"""
    with SessionLocal() as db:
        for ch in db.scalars(select(IMChannelRecord)).all():
            if (ch.extra or {}).get("notify_hitl") and ch.enabled:
                ch.enabled = False
        db.commit()


def _solo_notify_channel(client: TestClient, platform: str, prefix: str, **kw) -> dict:
    """本测试专用 notify_hitl 渠道：先停用既有同类，保证推送断言不被其他渠道污染。"""
    _disable_notify_channels()
    extra = {"notify_hitl": True, **(kw.pop("extra", {}) or {})}
    return _create_channel(client, platform, prefix, extra=extra, **kw)


def _poll(client: TestClient, task_id: str, states: set[str], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH)
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in states:
            return last
        time.sleep(0.15)
    return last


def _wait_push(min_count: int = 1, timeout: float = 10.0) -> list[dict]:
    """等任务引擎 worker 发出卡片推送（终态落库后异步推送，与状态轮询存在微小竞态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(SENT) >= min_count:
            return SENT
        time.sleep(0.1)
    return SENT


def _log_row(event_key: str) -> IMOutboundLogRecord | None:
    with SessionLocal() as db:
        return db.scalar(select(IMOutboundLogRecord)
                         .where(IMOutboundLogRecord.event_key == event_key))


def _feishu_card(payload: dict) -> dict:
    """群机器人 webhook 直发的飞书 interactive 卡片本体。"""
    assert payload["msg_type"] == "interactive"
    return payload["card"]


# ---------- 任务完成 → ✅ 回执卡片 ----------

def test_task_completed_pushes_done_card(client: TestClient):
    """agent.invoke 任务 COMPLETED → notify_task_done 被调：卡片标题「✅ 已完成」+
    正文含 task_id/类型/输出摘要；投递日志 done + 幂等键 done:{task_id}:COMPLETED。"""
    ch = _solo_notify_channel(client, "feishu", "fs-done")
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "faq-agent", "input": "hi"}})
    task_id = resp.json()["task_id"]
    task = _poll(client, task_id, {"COMPLETED"})
    assert task["state"] == "COMPLETED", task["result"]

    push = _wait_push(1)
    assert len(push) == 1  # 仅 notify_hitl 渠道收到，无其他出站
    assert push[0]["url"] == f"https://hook.example/{ch['name']}"
    card = _feishu_card(push[0]["payload"])
    assert card["header"]["title"]["content"] == "✅ 已完成"
    body = card["elements"][0]["text"]["content"]
    assert task_id in body and "agent.invoke" in body
    assert "输出：" in body and task["result"]["output"][:50] in body
    assert card["elements"][-1].get("actions") is None or not any(
        e.get("tag") == "action" for e in card["elements"])  # 终态无按钮
    rec = _log_row(f"done:{task_id}:COMPLETED")
    assert rec is not None and rec.direction == "out" and rec.status == "done"
    assert rec.channel_id == _channel_id(ch["name"])


def _channel_id(name: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(IMChannelRecord.id).where(IMChannelRecord.name == name))


# ---------- 任务失败 → ❌ 回执卡片 ----------

def test_task_failed_pushes_fail_card(client: TestClient):
    """agent.invoke 执行失败（未注册智能体 → handler 抛错）→ ❌ 卡片：
    标题「❌ 失败」+ 正文含 task_id 与错误摘要；幂等键带 FAILED 终态。"""
    _solo_notify_channel(client, "feishu", "fs-failcard")
    resp = client.post("/api/v1/tasks", headers=AUTH,
                       json={"type": "agent.invoke",
                             "payload": {"agent": "no-such-agent-m40", "input": "x"}})
    task_id = resp.json()["task_id"]
    task = _poll(client, task_id, {"FAILED"})
    assert task["state"] == "FAILED", task["result"]

    push = _wait_push(1)
    assert len(push) == 1
    card = _feishu_card(push[0]["payload"])
    assert card["header"]["title"]["content"] == "❌ 失败"
    body = card["elements"][0]["text"]["content"]
    assert task_id in body and "错误：" in body
    assert "未注册" in body  # handler 异常文案进入错误摘要
    rec = _log_row(f"done:{task_id}:FAILED")
    assert rec is not None and rec.status == "done"


# ---------- 挂钩范围 / 幂等 / 渠道过滤 / 失败入队 ----------

def test_non_agent_task_type_no_receipt(client: TestClient):
    """仅 agent.invoke/agent.hitl 任务类型挂钩回执：kb.ingest 等终态不推。"""
    _solo_notify_channel(client, "feishu", "fs-onlyagent")
    from eap.runtime.tasks import TaskEngine

    n = len(SENT)
    asyncio.run(TaskEngine()._notify_done("m40-kb-1", "kb.ingest", "COMPLETED",
                                          {"status": "ok"}))
    time.sleep(0.5)  # 推送窗口：非 agent 任务类型不应有任何出站
    assert len(SENT) == n


def test_notify_done_idempotent_event_key(client: TestClient):
    """幂等：event_key = done:{task_id}:{state}——同渠道重复推送跳过不重发。"""
    from eap.runtime import im_outbound as im_out

    ch = _solo_notify_channel(client, "feishu", "fs-done-idem")
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_task_done("m40-idem-1", "agent.invoke",
                                                     "COMPLETED", "已生成 3 条记录", db))
    assert pushed == [ch["name"]]
    n = len(SENT)
    with SessionLocal() as db:
        pushed2 = asyncio.run(im_out.notify_task_done("m40-idem-1", "agent.invoke",
                                                      "COMPLETED", "已生成 3 条记录", db))
    assert pushed2 == []  # 已 done 的同键投递 → 幂等跳过
    assert len(SENT) == n
    rec = _log_row("done:m40-idem-1:COMPLETED")
    assert rec is not None and rec.status == "done" and rec.attempts == 1


def test_channel_without_notify_not_pushed(client: TestClient):
    """非 notify 渠道（无开关/已停用）不推任何出站。"""
    from eap.runtime import im_outbound as im_out

    _disable_notify_channels()
    _create_channel(client, "feishu", "fs-nonotify40")  # extra 无开关
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_task_done("m40-nonotify-1", "agent.invoke",
                                                     "COMPLETED", "ok", db))
    assert pushed == []
    assert SENT == []
    assert _log_row("done:m40-nonotify-1:COMPLETED") is None


def test_notify_done_send_failure_enqueued(client: TestClient, monkeypatch):
    """首投失败 → pending 入既有重试队列（enqueue），不抛出。"""
    from eap.runtime import im_outbound as im_out

    _solo_notify_channel(client, "feishu", "fs-done-fail")

    async def boom(url, payload, headers=None, timeout=10.0):
        raise RuntimeError("net-boom-m40")

    monkeypatch.setattr(im_out, "post_json", boom)
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_task_done("m40-fail-1", "agent.invoke",
                                                     "FAILED", "boom", db))
    assert len(pushed) == 1  # 渠道命中但发送失败（入队重试）
    rec = _log_row("done:m40-fail-1:FAILED")
    assert rec is not None and rec.status == "pending" and rec.attempts == 1
    assert "net-boom-m40" in rec.error and rec.next_retry_at is not None
    # 置死信：避免后续测试触发后台重试循环时对该记录发起真实出站
    with SessionLocal() as db:
        row = db.get(IMOutboundLogRecord, rec.id)
        row.status = "dead"
        db.commit()


# ---------- 设备-用户配对（M40-A） ----------

def test_device_register_with_user_and_list(client: TestClient):
    """注册带 user（归属人标识）→ 注册响应与列表透出；缺省（API Key 通道无登录态）
    为 null；明细不落明文 key。"""
    r = client.post("/api/v1/auth/devices", headers=HEADERS,
                    json={"name": "m40-paired", "user": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"] == "alice" and body["api_key"].startswith("eap_d_")
    r = client.post("/api/v1/auth/devices", headers=HEADERS, json={"name": "m40-anon"})
    assert r.status_code == 200 and r.json()["user"] is None

    listing = client.get("/api/v1/auth/devices", headers=HEADERS).json()
    users = {d["name"]: d.get("user") for d in listing}
    assert users["m40-paired"] == "alice"
    assert users["m40-anon"] is None
    assert "api_key" not in __import__("json").dumps(listing)


def test_device_user_persisted_in_db(client: TestClient):
    """配对落库：device_user 列存归属人（设备↔用户绑定语义的地基）。"""
    from eap.models import ApiKey

    client.post("/api/v1/auth/devices", headers=HEADERS,
                json={"name": "m40-dbcheck", "user": "bob"})
    with SessionLocal() as db:
        row = db.scalar(select(ApiKey).where(ApiKey.note == "harness:m40-dbcheck"))
        assert row is not None and row.device_user == "bob"


# ---------- 迁移（api_keys.device_user 列） ----------

def _alembic_upgrade(tmp_path: Path, target: str, *, stamp: str | None = None) -> Path:
    """在独立临时库上跑 alembic 命令（env.py 经 get_settings() 读 EAP_DB_URL——
    运行期切换环境变量 + 清 lru_cache，结束恢复，不影响其他测试的库路由）。"""
    from alembic import command
    from alembic.config import Config

    from eap.config import get_settings

    db_path = tmp_path / "mig-eap.db"
    old = os.environ.get("EAP_DB_URL")
    os.environ["EAP_DB_URL"] = f"sqlite:///{db_path.as_posix()}"
    get_settings.cache_clear()
    try:
        cfg = Config()  # 不载 alembic.ini：跳过 fileConfig，避免扰动 pytest 日志配置
        cfg.set_main_option("script_location", str(_EAP_DIR / "migrations"))
        if stamp:
            command.stamp(cfg, stamp)
        command.upgrade(cfg, target)
    finally:
        if old is None:
            os.environ.pop("EAP_DB_URL", None)
        else:
            os.environ["EAP_DB_URL"] = old
        get_settings.cache_clear()
    return db_path


def test_migration_single_head_and_full_chain(tmp_path):
    """迁移链单头=新迁移 c1d3e5f7a9b1；全新库 upgrade head 全链通过，
    api_keys 落 device_user 列且版本表停在 head。"""
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text
    from sqlalchemy import inspect as sa_inspect

    sd = ScriptDirectory(str(_EAP_DIR / "migrations"))
    assert sd.get_heads() == ["c1d3e5f7a9b1"]

    db_path = _alembic_upgrade(tmp_path, "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        assert "device_user" in [c["name"] for c in sa_inspect(engine).get_columns("api_keys")]
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert ver == "c1d3e5f7a9b1"
    finally:
        engine.dispose()


def test_migration_add_column_on_legacy_db(tmp_path):
    """旧库增量路径：无 device_user 列的 api_keys（存量部署形态）stamp 到前一版本
    a5c7e9b1d3f5 后 upgrade head → 命中 add_column 分支，存量行不受影响。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy import inspect as sa_inspect

    db_path = tmp_path / "mig-eap.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url)
    with engine.begin() as conn:  # 旧形态 api_keys（M39 时点：无 device_user）
        conn.execute(text(
            "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key VARCHAR(128),"
            " key_hash VARCHAR(64), tenant_id INTEGER, note VARCHAR(128),"
            " enabled BOOLEAN)"))
        conn.execute(text("INSERT INTO api_keys (key_hash, tenant_id, note, enabled)"
                          " VALUES ('h-legacy', 1, 'harness:legacy', 1)"))
    engine.dispose()

    _alembic_upgrade(tmp_path, "head", stamp="a5c7e9b1d3f5")
    engine = create_engine(url)
    try:
        cols = [c["name"] for c in sa_inspect(engine).get_columns("api_keys")]
        assert "device_user" in cols
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT key_hash, note, device_user FROM api_keys WHERE note='harness:legacy'"
            )).one()
        assert row[0] == "h-legacy" and row[2] is None  # 存量行保留、新列 NULL
    finally:
        engine.dispose()


# ---------- 引擎挂钩端到端状态一致性 ----------

def test_done_receipt_after_recovered_task_shape(client: TestClient):
    """终态 result 形态兼容：result 无 output/error（如空结果）→ 推送不抛、正文仅状态行。"""
    from eap.runtime import im_outbound as im_out

    ch = _solo_notify_channel(client, "feishu", "fs-done-shape")
    with SessionLocal() as db:
        pushed = asyncio.run(im_out.notify_task_done("m40-shape-1", "agent.hitl",
                                                     "COMPLETED", "", db))
    assert pushed == [ch["name"]]
    card = _feishu_card(SENT[-1]["payload"])
    assert card["header"]["title"]["content"] == "✅ 已完成"
    assert "agent.hitl" in card["elements"][0]["text"]["content"]

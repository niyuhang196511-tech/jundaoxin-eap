"""对外 Webhook 推送测试（M31 任务组 B）：签名重算比对、事件→推送→done、
失败→重试→成功、超限→dead 死信、deliveries 过滤分页、手工重投、CRUD 权限/校验/
secret 不回显、事件不匹配不投递、试投通道。全部离线确定性：出站 HTTP 统一注入
fake transport，禁真实网络；后台重试循环禁用（schedule_retry_loop 置空），测试直驱
process_due。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import WebhookDeliveryRecord, WebhookEndpointRecord
from eap.runtime import webhooks as webhooks_rt
from eap.runtime.events import emit_event

from .conftest import AUTH
from .test_oidc import fake_idp, _make_id_token  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}

SENT: list[dict] = []  # 捕获的出站 HTTP 调用 {url, body(bytes), headers}


@pytest.fixture(autouse=True)
def capture_and_isolate(client, monkeypatch):
    """捕获出站 HTTP（离线确定性）；禁用后台重试循环（测试直驱 process_due）；
    测试结束后清空引擎快照与库内端点/投递，防残留端点跨测试收事件。"""
    SENT.clear()
    from eap.runtime import webhooks as wh

    async def fake_send(url, body, headers=None, timeout=10.0):
        SENT.append({"url": url, "body": body, "headers": headers or {}})
        return {"status": 200, "body": {"ok": True}}

    monkeypatch.setattr(wh, "send_webhook", fake_send)
    monkeypatch.setattr(wh, "base_delay", lambda: 50.0)  # pending 不在测试窗口内到期
    monkeypatch.setattr(wh, "poll_seconds", lambda: 3600.0)
    monkeypatch.setattr(wh, "schedule_retry_loop", lambda: None)
    yield
    engine = client.app.state.webhook_engine
    engine._endpoints.clear()  # 兜底：快照清空（真实 send 已随 monkeypatch 还原，不可再发）
    with SessionLocal() as db:
        for r in db.scalars(select(WebhookDeliveryRecord)).all():
            db.delete(r)
        for r in db.scalars(select(WebhookEndpointRecord)).all():
            db.delete(r)
        db.commit()


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_endpoint(client: TestClient, prefix: str, events: list[str],
                     secret: str | None = None, **kw) -> dict:
    name = _name(prefix)
    body = {"name": name, "url": f"https://hook.example/{name}", "events": events, **kw}
    if secret is not None:
        body["secret"] = secret
    r = client.post("/api/v1/webhooks", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _wait_deliveries(endpoint_id: int, timeout: float = 10.0,
                     status: str | None = None) -> list[WebhookDeliveryRecord]:
    """轮询某端点的投递记录（事件经应用循环异步消费，测试线程轮询 DB）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        with SessionLocal() as db:
            conds = [WebhookDeliveryRecord.endpoint_id == endpoint_id]
            if status:
                conds.append(WebhookDeliveryRecord.status == status)
            rows = db.scalars(select(WebhookDeliveryRecord).where(*conds)).all()
            if rows:
                return rows
        time.sleep(0.1)
    return []


def _delivery(rec_id: int) -> WebhookDeliveryRecord:
    with SessionLocal() as db:
        return db.get(WebhookDeliveryRecord, rec_id)


def _force_due(rec_id: int) -> None:
    """把投递记录置为立即到期（免等待退避，测试直驱 process_due）。"""
    with SessionLocal() as db:
        rec = db.get(WebhookDeliveryRecord, rec_id)
        rec.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()


# ---------- 签名与退避（纯函数） ----------

def test_backoff_growth(monkeypatch):
    """指数退避：base * 2^(attempts-1)，封顶 max。"""
    from eap.runtime import webhooks as wh

    monkeypatch.setattr(wh, "base_delay", lambda: 2.0)
    monkeypatch.setattr(wh, "max_delay", lambda: 300.0)
    assert (wh.backoff(1), wh.backoff(2), wh.backoff(3)) == (2.0, 4.0, 8.0)
    assert wh.backoff(10) == 300.0


def test_build_headers_signature_recomputable():
    """签名格式：X-EAP-Signature = hex(HMAC-SHA256(raw_body, secret))，接收方可逐字节重算。"""
    from eap.runtime import webhooks as wh

    body = json.dumps({"id": "evt-1", "type": "agent.run.completed"}, ensure_ascii=False).encode()
    headers = wh.build_headers("s3cret-wh", "evt-1", "agent.run.completed", body)
    expected = hmac.new(b"s3cret-wh", body, hashlib.sha256).hexdigest()
    assert headers["X-EAP-Signature"] == expected
    assert headers["X-EAP-Event-Id"] == "evt-1"
    assert headers["X-EAP-Event-Type"] == "agent.run.completed"
    float(headers["X-EAP-Timestamp"])  # 可解析时刻
    assert headers["Content-Type"] == "application/json"


# ---------- 事件 → 推送全链路 ----------

def test_event_push_signed_payload_done(client: TestClient):
    """订阅 agent.run.completed → emit 事件 → fake transport 收到带签名 payload（2xx→done）。"""
    ep = _create_endpoint(client, "wh31-hit", ["agent.run.completed"], secret="s3cret-wh")

    emit_event("agent.run.completed", tenant_id=1, data={"question": "如何创建知识库？"})
    rows = _wait_deliveries(ep["id"], status="done")
    assert rows, "匹配事件应产生 done 投递记录"
    rec = rows[0]
    assert rec.attempts == 1 and rec.response_status == 200 and rec.error == ""

    assert len(SENT) == 1
    call = SENT[0]
    assert call["url"] == ep["url"]
    # 签名：接收方按 raw_body + secret 重算比对
    expected_sig = hmac.new(b"s3cret-wh", call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-EAP-Signature"] == expected_sig
    assert call["headers"]["X-EAP-Event-Type"] == "agent.run.completed"
    assert call["headers"]["X-EAP-Event-Id"] == rec.event_id
    # payload：事件五元组原样 + meta 元数据
    payload = json.loads(call["body"])
    assert payload["id"] == rec.event_id
    assert payload["type"] == "agent.run.completed"
    assert payload["tenant_id"] == 1
    assert payload["data"] == {"question": "如何创建知识库？"}
    assert isinstance(payload["ts"], float)
    assert payload["meta"]["endpoint"] == ep["name"] and payload["meta"]["endpoint_id"] == ep["id"]
    # 审计：成功也落 webhook.deliver（detail 简短）
    audit_rows = client.get("/api/v1/audit", headers=AUTH,
                            params={"action": "webhook.deliver", "target": ep["name"]}).json()
    assert any(a["detail"].get("status") == "done" for a in audit_rows)

    assert client.delete(f"/api/v1/webhooks/{ep['id']}", headers=AUTH).status_code == 200


def test_event_not_matched_no_delivery(client: TestClient):
    """事件不匹配端点订阅 pattern 不投递；同端点匹配的事件照常投递（基础设施自证）。"""
    ep = _create_endpoint(client, "wh31-miss", ["wh31.other.*"], secret=None)

    emit_event("kb.document.indexed", tenant_id=1, data={"kb": "no-hit"})
    time.sleep(1.2)  # 给引擎消费留出时间（若误投递会落库）
    assert _wait_deliveries(ep["id"], timeout=0.1) == []

    emit_event("wh31.other.hit", tenant_id=1, data={"k": "v"})
    rows = _wait_deliveries(ep["id"], status="done")
    assert rows and rows[0].event_type == "wh31.other.hit"


# ---------- 重试队列：失败→重试→成功 / 超限→dead ----------

def test_retry_fail_then_succeed(client: TestClient, monkeypatch):
    from eap.runtime import webhooks as wh

    ep = _create_endpoint(client, "wh31-retry", ["wh31.retry.*"])
    calls = {"n": 0}

    async def flaky_send(url, body, headers=None, timeout=10.0):
        calls["n"] += 1
        if calls["n"] == 1:  # 首投失败（网络异常形态）
            raise RuntimeError("boom-network-wh31")
        SENT.append({"url": url, "body": body, "headers": headers or {}})
        return {"status": 200, "body": {"ok": True}}

    monkeypatch.setattr(wh, "send_webhook", flaky_send)
    emit_event("wh31.retry.evt", tenant_id=1, data={"k": "v"})
    rows = _wait_deliveries(ep["id"], status="pending")
    assert rows, "首投失败应落 pending"
    rec = rows[0]
    assert rec.attempts == 1 and "boom-network-wh31" in rec.error
    assert rec.next_retry_at is not None
    # 未到期不重投（退避窗口内扫描跳过，确定性无等待）
    assert asyncio.run(wh.process_due()) == 0
    # 到期后重投成功：pending → done（清错误）
    _force_due(rec.id)
    assert asyncio.run(wh.process_due()) == 1
    rec = _delivery(rec.id)
    assert rec.status == "done" and rec.attempts == 2 and rec.error == ""
    assert rec.response_status == 200
    assert len(SENT) == 1  # 重投的真实出站调用被捕获（带签名）
    expected_sig = hmac.new(b"", SENT[0]["body"], hashlib.sha256).hexdigest()
    assert SENT[0]["headers"]["X-EAP-Signature"] == expected_sig


def test_retry_exhausted_dead_letter(client: TestClient, monkeypatch):
    from eap.runtime import webhooks as wh

    monkeypatch.setattr(wh, "max_attempts", lambda: 2)  # 首投 + 1 次重投

    async def always_fail(url, body, headers=None, timeout=10.0):
        raise RuntimeError("dead-wh31")

    monkeypatch.setattr(wh, "send_webhook", always_fail)
    ep = _create_endpoint(client, "wh31-dead", ["wh31.dead.*"])
    emit_event("wh31.dead.evt", tenant_id=1, data={"k": "v"})
    rows = _wait_deliveries(ep["id"], status="pending")
    assert rows and rows[0].attempts == 1
    rec_id = rows[0].id
    _force_due(rec_id)
    assert asyncio.run(wh.process_due()) == 1
    rec = _delivery(rec_id)
    assert rec.status == "dead" and rec.attempts == 2 and "dead-wh31" in rec.error
    # 死信不再进入扫描
    _force_due(rec_id)
    assert asyncio.run(wh.process_due()) == 0
    assert _delivery(rec_id).status == "dead"


# ---------- 投递记录查询 / 手工重投 ----------

def test_deliveries_query_filter_pagination(client: TestClient):
    ep = _create_endpoint(client, "wh31-q", ["wh31.q.*"])
    with SessionLocal() as db:
        db.add(WebhookDeliveryRecord(endpoint_id=ep["id"], event_id="e-done",
                                     event_type="wh31.q.a", payload={}, attempts=1,
                                     status="done", response_status=200))
        db.add(WebhookDeliveryRecord(endpoint_id=ep["id"], event_id="e-dead",
                                     event_type="wh31.q.b", payload={}, attempts=5,
                                     status="dead", error="HTTP 500"))
        db.commit()
    # 按 status 过滤
    r = client.get("/api/v1/webhooks/deliveries", headers=AUTH,
                   params={"endpoint_id": ep["id"], "status": "dead"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["event_id"] == "e-dead"
    assert body["items"][0]["error"] == "HTTP 500" and body["items"][0]["attempts"] == 5
    # done 过滤 + 分页
    r = client.get("/api/v1/webhooks/deliveries", headers=AUTH,
                   params={"endpoint_id": ep["id"], "status": "done", "limit": 1, "offset": 0})
    body = r.json()
    assert body["total"] == 1 and len(body["items"]) == 1 and body["items"][0]["event_id"] == "e-done"
    # 非法状态 → 400
    assert client.get("/api/v1/webhooks/deliveries", headers=AUTH,
                      params={"status": "bogus"}).status_code == 400


def test_redeliver_dead_delivery(client: TestClient):
    """死信手工重投：attempts 重置 → pending 到期 → 重投成功 done；done 不可重投（409）。"""
    ep = _create_endpoint(client, "wh31-rd", ["wh31.rd.*"])
    with SessionLocal() as db:
        rec = WebhookDeliveryRecord(endpoint_id=ep["id"], event_id="e-rd",
                                    event_type="wh31.rd.evt",
                                    payload={"id": "e-rd", "type": "wh31.rd.evt",
                                             "tenant_id": None, "data": {"k": "v"}, "ts": 0.0},
                                    attempts=5, status="dead", error="HTTP 500")
        db.add(rec)
        db.commit()
        rec_id = rec.id
    r = client.post(f"/api/v1/webhooks/deliveries/{rec_id}/redeliver", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["attempts"] == 0
    # 重投成功（重试循环被禁用，测试直驱 process_due）
    assert asyncio.run(webhooks_rt.process_due()) == 1
    rec = _delivery(rec_id)
    assert rec.status == "done" and rec.attempts == 1 and rec.error == ""
    assert len(SENT) == 1 and SENT[0]["url"] == ep["url"]
    # 审计 webhook.redeliver
    audit_rows = client.get("/api/v1/audit", headers=AUTH,
                            params={"action": "webhook.redeliver"}).json()
    assert any(a["target"] == str(rec_id) for a in audit_rows)
    # 已成功不可重投 → 409；未知 → 404
    assert client.post(f"/api/v1/webhooks/deliveries/{rec_id}/redeliver",
                       headers=AUTH).status_code == 409
    assert client.post("/api/v1/webhooks/deliveries/999999/redeliver",
                       headers=AUTH).status_code == 404


# ---------- 试投通道 ----------

def test_manual_test_push(client: TestClient):
    """POST /{id}/test：构造样例事件走真实投递通道（不经 pattern 匹配）。"""
    ep = _create_endpoint(client, "wh31-test", ["wh31.test.*"])
    r = client.post(f"/api/v1/webhooks/{ep['id']}/test", headers=HEADERS,
                    json={"data": {"hello": "wh31"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == ep["name"] and body["delivery_id"] > 0
    rows = _wait_deliveries(ep["id"], status="done")
    assert rows and rows[0].event_type == "webhook.test"
    payload = json.loads(SENT[0]["body"])
    assert payload["type"] == "webhook.test" and payload["data"] == {"hello": "wh31"}
    assert payload["id"] == body["event_id"]
    assert client.post("/api/v1/webhooks/999999/test", headers=AUTH,
                       json={"data": {}}).status_code == 404


# ---------- CRUD：权限 / 校验 / secret 不回显 ----------

def _token(roles: list[str]) -> str:
    claims = {"iss": "https://idp.example", "sub": "wh31@corp", "exp": int(time.time()) + 600,
              "tenant_id": 1, "roles": roles}
    return _make_id_token(claims)


def test_crud_rbac_and_validation(client: TestClient, fake_idp):  # noqa: F811
    member = _token(roles=["member"])
    body = {"name": _name("wh31-crud"), "url": "https://hook.example/crud",
            "events": ["agent.run.completed"]}
    # member 写/读 → 403（整路由 admin-only）
    assert client.post("/api/v1/webhooks", headers={"Authorization": f"Bearer {member}"},
                       json=body).status_code == 403
    assert client.get("/api/v1/webhooks",
                      headers={"Authorization": f"Bearer {member}"}).status_code == 403
    assert client.get("/api/v1/webhooks/deliveries",
                      headers={"Authorization": f"Bearer {member}"}).status_code == 403
    # 缺凭证 → 401
    assert client.post("/api/v1/webhooks", json=body).status_code == 401
    # admin 创建 → 200；secret 不回显
    r = client.post("/api/v1/webhooks", headers=HEADERS,
                    json={**body, "secret": "sk-plain-wh31"})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["enabled"] is True and view["has_secret"] is True
    assert "sk-plain-wh31" not in json.dumps(view) and "secret" not in view
    eid = view["id"]
    assert view["events"] == ["agent.run.completed"]
    # 重名 → 409
    assert client.post("/api/v1/webhooks", headers=HEADERS, json=body).status_code == 409
    # 校验：URL 非 http(s) → 422；events 为空 → 422；pattern 非法 → 400
    assert client.post("/api/v1/webhooks", headers=HEADERS,
                       json={"name": _name("wh31-bad"), "url": "ftp://x/y",
                             "events": ["a.*"]}).status_code == 422
    assert client.post("/api/v1/webhooks", headers=HEADERS,
                       json={"name": _name("wh31-bad"), "url": "https://x/y",
                             "events": []}).status_code == 422
    assert client.post("/api/v1/webhooks", headers=HEADERS,
                       json={"name": _name("wh31-bad"), "url": "https://x/y",
                             "events": ["bad pattern!"]}).status_code == 400
    # PATCH：改 URL + 通配 pattern
    r = client.patch(f"/api/v1/webhooks/{eid}", headers=HEADERS,
                     json={"url": "https://hook.example/crud-v2", "events": ["task.*"]})
    assert r.status_code == 200 and r.json()["url"].endswith("crud-v2")
    assert r.json()["events"] == ["task.*"]
    # 列表可见 → DELETE
    assert any(x["id"] == eid for x in client.get("/api/v1/webhooks", headers=AUTH).json())
    assert client.delete(f"/api/v1/webhooks/{eid}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/v1/webhooks/{eid}", headers=AUTH).status_code == 404


def test_secret_encrypted_at_rest(client: TestClient, monkeypatch):
    """EAP_SECRET_KEY 配置时 secret Fernet 加密落库（enc1: 前缀），可解密回明文。"""
    from eap import security_crypto
    from eap.config import get_settings

    monkeypatch.setenv("EAP_SECRET_KEY", "m31-wh-secret")
    get_settings.cache_clear()
    try:
        ep = _create_endpoint(client, "wh31-enc", ["wh31.enc.*"], secret="sk-plain-enc")
        row = db_row = None
        with SessionLocal() as db:
            db_row = db.scalar(select(WebhookEndpointRecord)
                               .where(WebhookEndpointRecord.id == ep["id"]))
            assert db_row.secret.startswith("enc1:")
            assert security_crypto.decrypt_secret(db_row.secret) == "sk-plain-enc"
        assert "sk-plain-enc" not in json.dumps(ep)
    finally:
        get_settings.cache_clear()

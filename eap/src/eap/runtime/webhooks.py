"""对外 Webhook 推送引擎（docs/unfinished §三十五，M31 任务组 B）：事件 → HMAC 签名 → HTTP POST。

- 启动（lifespan，照 triggers.py 生命周期）订阅事件总线（"*" 全收），消费时按端点
  订阅 pattern（fnmatch 通配）匹配分发；端点 CRUD 后经 reload() 即时生效
- payload：事件 {id, type, tenant_id, data, ts} 原样 + meta 元数据（端点/投递时刻）；
  签名 X-EAP-Signature = hex(HMAC-SHA256(raw_body, secret))，与入站 webhook（M30）对称；
  附 X-EAP-Event-Id / X-EAP-Event-Type / X-EAP-Timestamp 供接收方去重与新鲜度校验
- 结果落 WebhookDeliveryRecord（成功失败均落库）；失败按指数退避重投
  （base*2^(attempts-1) 封顶，EAP_WEBHOOK_* 可配，max_attempts 次后 dead 死信）
  ——进程内 asyncio 循环自包含（照 im_outbound 模式，惰性启动，无 Redis 依赖）
- HTTP 发送函数可注入（默认 httpx.AsyncClient；测试 monkeypatch 注入 fake，禁真实网络）
- 每次投递审计 webhook.deliver（成功也记，detail 简短）
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import WebhookDeliveryRecord, WebhookEndpointRecord
from ..observability import audit
from ..security_crypto import decrypt_secret
from .events import bus

log = logging.getLogger("eap.webhooks")

_SCAN_BATCH = 20  # 单轮扫描的最大重投条数


def _utcnow() -> datetime:
    """库内统一存 naive UTC（与 audit.py 约定一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- 重试参数（EAP_WEBHOOK_*，测试可 monkeypatch 覆盖） ----------

def max_attempts() -> int:
    from ..config import get_settings

    return get_settings().webhook_max_attempts


def base_delay() -> float:
    from ..config import get_settings

    return get_settings().webhook_base_seconds


def max_delay() -> float:
    from ..config import get_settings

    return get_settings().webhook_max_seconds


def timeout_s() -> float:
    from ..config import get_settings

    return get_settings().webhook_timeout_s


def poll_seconds() -> float:
    from ..config import get_settings

    return get_settings().im_retry_poll_seconds


def backoff(attempts: int) -> float:
    """指数退避：base * 2^(attempts-1)，封顶 max_delay（秒）。"""
    return min(base_delay() * (2 ** max(0, attempts - 1)), max_delay())


# ---------- HTTP 发送（默认 httpx，测试注入 fake） ----------

async def send_webhook(url: str, body: bytes, headers: dict | None = None,
                       timeout: float = 10.0) -> dict:
    """原始字节发送（签名需逐字节可复算，故不走 json= 再序列化）。

    统一返回 {"status", "body"}；网络异常直接抛出（调用方入重试队列）。
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.post(url, content=body, headers=headers)
        try:
            parsed = json.loads(resp.text)
        except (json.JSONDecodeError, TypeError):
            parsed = resp.text[:500]
        return {"status": resp.status_code, "body": parsed}


def build_headers(secret: str, event_id: str, event_type: str, body: bytes) -> dict:
    """推送头：X-EAP-Signature = hex(HMAC-SHA256(raw_body, secret))（raw_body 逐字节可复算）。"""
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-EAP-Signature": signature,
            "X-EAP-Event-Id": event_id, "X-EAP-Event-Type": event_type,
            "X-EAP-Timestamp": str(time.time())}


def build_payload(event: dict, endpoint_name: str, endpoint_id: int) -> dict:
    """推送体：事件五元组原样 + meta 元数据（不改动事件字段，消费方按需取用）。"""
    payload = {k: event.get(k) for k in ("id", "type", "tenant_id", "data", "ts")}
    payload["meta"] = {"endpoint": endpoint_name, "endpoint_id": endpoint_id,
                       "sent_at": _utcnow().isoformat(timespec="seconds")}
    return payload


@dataclass
class EndpointSnapshot:
    """端点内存快照（DB 行的解密后投影，脱离会话使用）。"""

    id: int
    name: str
    tenant_id: int | None
    url: str
    secret: str = ""  # 解密后明文（HMAC 签名用；未配置则空 = 不签名头置空串）
    events: list[str] = field(default_factory=list)

    def matches(self, event_type: str) -> bool:
        """任一订阅 pattern 命中即投递（fnmatch 通配，与总线订阅语义一致）。"""
        return any(fnmatch.fnmatchcase(event_type, p) for p in self.events)


def snapshot_of(record: WebhookEndpointRecord) -> EndpointSnapshot:
    """DB 行 → 端点快照（secret 解密；events JSON 字符串解析，坏值跳过该 pattern）。"""
    try:
        patterns = json.loads(record.events or "[]")
        if not isinstance(patterns, list):
            patterns = []
    except ValueError:
        patterns = []
    return EndpointSnapshot(
        id=record.id, name=record.name, tenant_id=record.tenant_id, url=record.url,
        secret=decrypt_secret(record.secret) or "",
        events=[str(p) for p in patterns if p],
    )


class WebhookEngine:
    """Webhook 推送引擎：总线订阅 → pattern 匹配 → 签名推送 → 投递记录 + 重试队列。"""

    def __init__(self) -> None:
        self._endpoints: dict[int, EndpointSnapshot] = {}
        self._queue: asyncio.Queue | None = None  # 全量订阅队列（消费时按端点 pattern 匹配）
        self._consumer: asyncio.Task | None = None

    # ---------- 生命周期（照 triggers.py 写法） ----------

    def reload(self) -> int:
        """从 DB 重载 enabled 端点（CRUD 后即时生效）。

        消费任务须在事件循环内创建——CRUD 端点均为 async def（循环上执行）；
        无循环上下文（防御）仅刷新内存快照，订阅留待下次循环内 reload。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._consumer is not None:
            self._consumer.cancel()
            self._consumer = None
        if self._queue is not None:
            bus.unsubscribe(self._queue)
            self._queue = None
        with SessionLocal() as db:
            for record in db.scalars(select(WebhookEndpointRecord)
                                     .where(WebhookEndpointRecord.enabled == True)).all():  # noqa: E712
                try:
                    self._endpoints[record.id] = snapshot_of(record)
                except Exception as e:
                    log.warning("端点 %s 快照失败: %s", record.name, e)
        if loop is not None:
            self._queue = bus.subscribe("*")
            self._consumer = asyncio.create_task(self._consume(self._queue))
        return len(self._endpoints)

    async def start(self) -> None:
        count = self.reload()
        log.info("Webhook 推送引擎启动：%d 个端点", count)

    async def stop(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None
        if self._queue is not None:
            bus.unsubscribe(self._queue)
            self._queue = None
        self._endpoints.clear()

    # ---------- 事件消费 ----------

    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            try:
                await self.dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("事件 %s 分发异常: %s", event.get("type"), e)

    async def dispatch(self, event: dict) -> int:
        """事件 → 匹配端点逐一推送；返回投递端点数（任何单端点失败不阻断其余端点）。"""
        event_type = str(event.get("type") or "")
        sent = 0
        for snap in list(self._endpoints.values()):
            if not snap.matches(event_type):
                continue
            try:
                await self.push(snap, event)
                sent += 1
            except Exception as e:
                log.warning("端点 %s 投递异常: %s", snap.name, e)
        return sent

    async def push(self, snap: EndpointSnapshot, event: dict) -> int:
        """对单端点执行一次投递（首投 attempts=1）：2xx → done；失败 → 退避重试 / 超限 dead。

        返回投递记录 id；先发后落库（HTTP 期间不持 DB 会话）。
        """
        payload = build_payload(event, snap.name, snap.id)
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = build_headers(snap.secret, str(event.get("id") or ""), str(event.get("type") or ""),
                                body)
        status, response_status, error = "pending", None, ""
        try:
            result = await send_webhook(snap.url, body, headers, timeout_s())
            code = int((result or {}).get("status") or 0)
            if not 200 <= code < 300:
                raise RuntimeError(f"HTTP {code}")
            status, response_status = "done", code
        except Exception as e:
            error = str(e)[:500]
            if max_attempts() <= 1:
                status = "dead"
        delivery_id = 0
        next_retry_at = None
        if status == "pending":
            next_retry_at = _utcnow() + timedelta(seconds=backoff(1))
        with SessionLocal() as db:
            rec = WebhookDeliveryRecord(
                endpoint_id=snap.id, event_id=str(event.get("id") or ""),
                event_type=str(event.get("type") or ""), payload=payload,
                attempts=1, next_retry_at=next_retry_at, status=status,
                response_status=response_status, error=error)
            db.add(rec)
            db.commit()
            delivery_id = rec.id
        if status == "pending":
            schedule_retry_loop()
        audit.record("webhook.deliver", actor="system", target=snap.name,
                     detail={"event": str(event.get("type") or ""), "status": status,
                             "delivery_id": delivery_id})
        if status == "dead":
            log.warning("Webhook 投递死信 endpoint=%s event=%s：%s", snap.name,
                        event.get("id"), error)
        return delivery_id

    # ---------- 手工试投（API /{id}/test 用） ----------

    async def test_push(self, snap: EndpointSnapshot, data: dict) -> dict:
        """构造样例事件走真实投递通道（不经 pattern 匹配），返回投递结果。"""
        event = bus.build_event("webhook.test", snap.tenant_id, data)
        delivery_id = await self.push(snap, event)
        return {"event_id": event["id"], "event_type": event["type"], "delivery_id": delivery_id}


# ---------- 重试队列：扫描重投（照 im_outbound 模式） ----------

async def _attempt(db, rec: WebhookDeliveryRecord) -> None:
    """单条重投尝试：按端点当前 URL/secret 重新签名发送；成功 done / 超限 dead / 否则退避。"""
    rec.attempts += 1
    snap = None
    with SessionLocal() as edb:
        record = edb.get(WebhookEndpointRecord, rec.endpoint_id)
        if record is not None and record.enabled:
            snap = snapshot_of(record)
    try:
        if snap is None:
            raise RuntimeError(f"EAP-4004 端点 {rec.endpoint_id} 不存在或未启用")
        payload = dict(rec.payload or {})
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = build_headers(snap.secret, rec.event_id, rec.event_type, body)
        result = await send_webhook(snap.url, body, headers, timeout_s())
        code = int((result or {}).get("status") or 0)
        if not 200 <= code < 300:
            raise RuntimeError(f"HTTP {code}")
        rec.status = "done"
        rec.response_status = code
        rec.error = ""
    except Exception as e:  # 投递失败是队列常态，不随异常中断
        rec.error = str(e)[:500]
        if rec.attempts >= max_attempts():
            rec.status = "dead"
            log.warning("Webhook 投递死信 id=%s endpoint=%s：%s", rec.id, rec.endpoint_id, rec.error)
        else:
            rec.status = "pending"
            rec.next_retry_at = _utcnow() + timedelta(seconds=backoff(rec.attempts))


async def process_due() -> int:
    """扫描并尝试全部到期重投（pending 且 next_retry_at<=now）；返回处理条数。

    测试可直接调用驱动一轮（免等待后台循环）；重试循环每轮亦经此函数。
    """
    now = _utcnow()
    processed = 0
    with SessionLocal() as db:
        recs = db.scalars(select(WebhookDeliveryRecord)
                          .where(WebhookDeliveryRecord.status == "pending",
                                 WebhookDeliveryRecord.next_retry_at <= now)
                          .order_by(WebhookDeliveryRecord.next_retry_at)
                          .limit(_SCAN_BATCH)).all()
        for rec in recs:
            await _attempt(db, rec)
            processed += 1
        db.commit()
    return processed


# ---------- 重试循环（惰性启动，随事件循环存活；照 im_outbound） ----------

_task: asyncio.Task | None = None
_wakeup: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def schedule_retry_loop() -> None:
    """确保重试循环在当前事件循环运行（惰性启动；已在跑则仅唤醒提前扫描）。"""
    global _task, _wakeup, _loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步上下文（测试直驱 process_due）：无需后台循环
    if _loop is loop and _task is not None and not _task.done():
        _wakeup.set()
        return
    _loop = loop
    _wakeup = asyncio.Event()
    _task = loop.create_task(_retry_loop())


async def _retry_loop() -> None:
    """重试主循环：到期 pending 指数退避重投；空闲定时轮询 + 入队事件唤醒。"""
    while True:
        try:
            await process_due()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 单轮失败不影响循环存活
            log.warning("Webhook 重试轮询异常: %s", e)
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=poll_seconds())
        except TimeoutError:
            pass
        _wakeup.clear()


__all__ = ["WebhookEngine", "EndpointSnapshot", "snapshot_of", "process_due",
           "schedule_retry_loop", "build_headers", "build_payload", "send_webhook"]

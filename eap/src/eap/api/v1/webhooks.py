"""对外 Webhook 推送 API（M31 任务组 B）：端点 CRUD / 投递记录查询 / 手工重投 / 试投。

- CRUD + deliveries 全部 require_admin（写操作落审计 webhook.create/update/delete/
  redeliver，参考 triggers.py / releases.py）
- 校验：URL 须 http(s)；events 非空且 pattern 合法（fnmatch 字符集）；secret Fernet
  加密落库（M8），查询只回 has_secret 不回显
- POST /{id}/test：构造样例事件（type=webhook.test）走真实投递通道（签名/重试一致）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import WebhookDeliveryRecord, WebhookEndpointRecord
from ...observability import audit
from ...security_crypto import encrypt_secret
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/webhooks",
                           dependencies=[fastapi.Depends(resolve_tenant),
                                         fastapi.Depends(require_api_key),
                                         fastapi.Depends(require_admin)])

# 订阅 pattern 合法字符（fnmatch 语义：字面量 + * ? [seq] [!seq]）
_PATTERN = re.compile(r"^[A-Za-z0-9_.\-*?\[\]!]{1,128}$")


class WebhookCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    url: str = Field(max_length=512, pattern=r"^https?://\S+$")
    events: list[str] = Field(min_length=1, max_length=16)
    secret: str | None = Field(default=None, max_length=256,
                               description="HMAC 签名密钥（Fernet 加密存储，查询不回显）")
    tenant_id: int | None = None
    enabled: bool = True


class WebhookPatch(BaseModel):
    url: str | None = Field(default=None, max_length=512, pattern=r"^https?://\S+$")
    events: list[str] | None = Field(default=None, min_length=1, max_length=16)
    secret: str | None = Field(default=None, max_length=256)
    tenant_id: int | None = None
    enabled: bool | None = None


class TestPushRequest(BaseModel):
    data: dict = Field(default_factory=dict, description="样例事件 data（透传为推送 payload）")


def _view(r: WebhookEndpointRecord) -> dict:
    try:
        events = json.loads(r.events or "[]")
        if not isinstance(events, list):
            events = []
    except ValueError:
        events = []
    return {"id": r.id, "name": r.name, "url": r.url, "events": events,
            "has_secret": bool(r.secret), "tenant_id": r.tenant_id, "enabled": r.enabled,
            "created_at": str(r.created_at), "updated_at": str(r.updated_at)}


def _validate_events(body: WebhookCreate | WebhookPatch) -> list[str] | None:
    """events 语义校验：非空、pattern 字符集合法；返回规范化后的列表（无变更返回 None）。"""
    events = body.events
    if events is None:
        return None
    for p in events:
        if not _PATTERN.fullmatch(p or ""):
            raise fastapi.HTTPException(status_code=400,
                                        detail=f"EAP-4000 订阅 pattern 非法: {p!r}")
    return [p.strip() for p in events if p.strip()]


def _engine(request: fastapi.Request):
    return getattr(request.app.state, "webhook_engine", None)


@router.get("")
def list_endpoints(db: Session = fastapi.Depends(get_db)):
    return [_view(r) for r in db.scalars(
        select(WebhookEndpointRecord).order_by(WebhookEndpointRecord.id.desc())).all()]


@router.post("")
async def create_endpoint(body: WebhookCreate, request: fastapi.Request,
                          db: Session = fastapi.Depends(get_db)):
    events = _validate_events(body)
    if db.scalar(select(WebhookEndpointRecord).where(WebhookEndpointRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-2002 端点 {body.name} 已存在")
    record = WebhookEndpointRecord(
        name=body.name, url=body.url, events=json.dumps(events, ensure_ascii=False),
        secret=encrypt_secret(body.secret),  # M8 静态加密（未配置密钥时明文兼容）
        tenant_id=body.tenant_id, enabled=body.enabled)
    db.add(record)
    db.commit()
    audit.record("webhook.create", actor=audit.actor_of(request), target=record.name,
                 detail={"url": record.url, "events": events},  # URL 非敏感字段；secret 从不入审计
                 trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()  # 推送引擎即时生效
    return _view(record)


@router.patch("/{endpoint_id}")
async def patch_endpoint(endpoint_id: int, body: WebhookPatch, request: fastapi.Request,
                         db: Session = fastapi.Depends(get_db)):
    record = db.get(WebhookEndpointRecord, endpoint_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 端点 {endpoint_id} 不存在")
    events = _validate_events(body)
    changes: list[str] = []
    for field, value in (("url", body.url), ("tenant_id", body.tenant_id),
                         ("enabled", body.enabled), ("events", events)):
        if value is None:
            continue
        if field == "events":
            value = json.dumps(value, ensure_ascii=False)
        if getattr(record, field, None) != value:
            changes.append(field)
        setattr(record, field, value)
    if body.secret is not None:
        record.secret = encrypt_secret(body.secret)  # 更新密钥同样加密落库
        changes.append("secret")
    db.commit()
    audit.record("webhook.update", actor=audit.actor_of(request), target=record.name,
                 detail={"changes": sorted(changes)}, trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()
    return _view(record)


@router.delete("/{endpoint_id}")
async def delete_endpoint(endpoint_id: int, request: fastapi.Request,
                          db: Session = fastapi.Depends(get_db)):
    record = db.get(WebhookEndpointRecord, endpoint_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 端点 {endpoint_id} 不存在")
    name = record.name
    db.delete(record)  # 投递记录随 FK 级联语义由 DB 决定；查询端点过滤已不存在者自然为空
    db.commit()
    audit.record("webhook.delete", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()
    return {"name": name, "status": "deleted"}


# ---------- 投递记录（死信可查询） ----------

@router.get("/deliveries")
def list_deliveries(endpoint_id: int | None = None, status: str | None = None,
                    limit: int = 50, offset: int = 0,
                    db: Session = fastapi.Depends(get_db)):
    """投递记录查询：按端点/状态过滤 + 分页（新→旧）；status=pending|done|dead。"""
    if status is not None and status not in ("pending", "done", "dead"):
        raise fastapi.HTTPException(status_code=400,
                                    detail=f"EAP-4000 非法状态: {status}（pending|done|dead）")
    conds = []
    if endpoint_id is not None:
        conds.append(WebhookDeliveryRecord.endpoint_id == endpoint_id)
    if status is not None:
        conds.append(WebhookDeliveryRecord.status == status)
    total = db.scalar(select(func.count()).select_from(WebhookDeliveryRecord).where(*conds)) or 0
    stmt = (select(WebhookDeliveryRecord).where(*conds)
            .order_by(WebhookDeliveryRecord.id.desc())
            .limit(max(1, min(limit, 200))).offset(max(0, offset)))
    items = [{"id": r.id, "endpoint_id": r.endpoint_id, "event_id": r.event_id,
              "event_type": r.event_type, "attempts": r.attempts, "status": r.status,
              "response_status": r.response_status, "error": r.error,
              "next_retry_at": str(r.next_retry_at) if r.next_retry_at else None,
              "created_at": str(r.created_at)}
             for r in db.scalars(stmt).all()]
    return {"total": total, "items": items}


@router.post("/deliveries/{delivery_id}/redeliver")
async def redeliver(delivery_id: int, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    """手工重投（dead/pending）：attempts 重置为 0，置为立即到期 pending，由重试循环投递。"""
    record = db.get(WebhookDeliveryRecord, delivery_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 投递记录 {delivery_id} 不存在")
    if record.status == "done":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-6002 投递 {delivery_id} 已成功，无需重投")
    record.attempts = 0
    record.status = "pending"
    record.error = ""
    record.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    audit.record("webhook.redeliver", actor=audit.actor_of(request), target=str(delivery_id),
                 detail={"endpoint_id": record.endpoint_id, "event_type": record.event_type},
                 trace_id=getattr(request.state, "trace_id", ""))
    from ...runtime import webhooks as webhooks_rt

    webhooks_rt.schedule_retry_loop()  # 唤醒重试循环尽快投递（无循环时测试直驱 process_due）
    return {"id": record.id, "status": record.status, "attempts": record.attempts}


# ---------- 手工试投 ----------

@router.post("/{endpoint_id}/test")
async def test_push(endpoint_id: int, body: TestPushRequest, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    """构造样例事件走真实投递通道（admin）：签名/重试/审计与真实事件完全一致。"""
    record = db.get(WebhookEndpointRecord, endpoint_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 端点 {endpoint_id} 不存在")
    engine = _engine(request)
    if engine is None:
        raise fastapi.HTTPException(status_code=503, detail="EAP-4005 Webhook 推送引擎未启动")
    from ...runtime.webhooks import snapshot_of

    result = await engine.test_push(snapshot_of(record), body.data)
    audit.record("webhook.test", actor=audit.actor_of(request), target=record.name,
                 detail={"event_id": result["event_id"], "delivery_id": result["delivery_id"]},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"endpoint": record.name, **result}

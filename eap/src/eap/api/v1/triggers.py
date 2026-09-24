"""触发器 API（M30 事件中心）：规则 CRUD（admin + 审计）/ 入站 webhook（公开端点）/ 手工试触发。

- CRUD 全部 require_admin（写操作落审计 trigger.create/update/delete，参考 releases.py）
- POST /webhook/{rule_id}：公开端点（无凭证依赖，签名即凭证）——
  X-EAP-Signature = hex(HMAC-SHA256(raw_body, 规则 secret))，错签 401；
  X-EAP-Event-Id 重放去重（进程内 LRU，多副本部署为尽力而为语义）
- POST /{rule_id}/test-fire：admin 手工注入样例 payload 验证触发链路（audit source=manual）
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import TriggerRuleRecord
from ...observability import audit
from ...security_crypto import encrypt_secret
from ..deps import require_admin, require_api_key, resolve_tenant
from ...runtime.triggers import snapshot_of

# 管理面：规则 CRUD + 试触发（admin）；公开面：webhook 单独挂载（无鉴权依赖）
router = fastapi.APIRouter(prefix="/api/v1/triggers",
                           dependencies=[fastapi.Depends(resolve_tenant),
                                         fastapi.Depends(require_api_key),
                                         fastapi.Depends(require_admin)])
public_router = fastapi.APIRouter(prefix="/api/v1/triggers")


class TriggerCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    tenant_id: int | None = None
    source: str = Field(pattern=r"^(event|cron|webhook)$")
    event_type: str | None = Field(default=None, max_length=128)
    match: dict | None = None
    cron: str | None = Field(default=None, max_length=64,
                             description="5 段 cron 表达式（UTC），source=cron 必填")
    secret: str | None = Field(default=None, max_length=256,
                               description="webhook HMAC 密钥（Fernet 加密存储，查询不回显）")
    target_type: str = Field(default="agent", pattern=r"^(agent|workflow|connector)$")
    target_name: str = Field(min_length=1, max_length=64)
    input_mode: str = Field(default="payload", pattern=r"^(payload|template)$")
    template: dict | str | None = None
    min_interval_s: int = Field(default=0, ge=0, le=86400)
    enabled: bool = True


class TriggerPatch(BaseModel):
    event_type: str | None = Field(default=None, max_length=128)
    match: dict | None = None
    cron: str | None = Field(default=None, max_length=64)
    secret: str | None = Field(default=None, max_length=256)
    target_type: str | None = Field(default=None, pattern=r"^(agent|workflow|connector)$")
    target_name: str | None = Field(default=None, min_length=1, max_length=64)
    input_mode: str | None = Field(default=None, pattern=r"^(payload|template)$")
    template: dict | str | None = None
    min_interval_s: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None


class TestFireRequest(BaseModel):
    data: dict = Field(default_factory=dict, description="样例触发数据（透传为目标输入/模板变量）")


def _view(rule: TriggerRuleRecord) -> dict:
    return {"id": rule.id, "name": rule.name, "tenant_id": rule.tenant_id,
            "source": rule.source, "event_type": rule.event_type, "match": rule.match,
            "cron": rule.cron, "has_secret": bool(rule.secret),
            "target_type": rule.target_type, "target_name": rule.target_name,
            "input_mode": rule.input_mode, "template": rule.template,
            "min_interval_s": rule.min_interval_s, "enabled": rule.enabled,
            "created_at": str(rule.created_at), "updated_at": str(rule.updated_at)}


def _validate(db: Session, body: TriggerCreate) -> None:
    """来源语义校验：event 必填 event_type；cron 必填且合法；名称唯一。"""
    if db.scalar(select(TriggerRuleRecord).where(TriggerRuleRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 触发器 {body.name} 已存在")
    if body.source == "event" and not body.event_type:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 source=event 须指定 event_type")
    if body.source == "cron":
        if not body.cron:
            raise fastapi.HTTPException(status_code=400, detail="EAP-4000 source=cron 须指定 cron 表达式")
        from croniter import croniter

        try:
            croniter(body.cron)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 cron 无效: {e}") from e


def _engine(request: fastapi.Request):
    return getattr(request.app.state, "trigger_engine", None)


@router.get("")
def list_triggers(db: Session = fastapi.Depends(get_db)):
    rules = db.scalars(select(TriggerRuleRecord)
                       .order_by(TriggerRuleRecord.id.desc())).all()
    return [_view(r) for r in rules]


@router.get("/event-types")
def list_event_types():
    """事件类型目录（M49-E1）：供触发器/Webhook 订阅表单做选项提示。

    目录为文档性质（runtime/events.EVENT_CATALOG，与全仓 emit 点核实同步）；
    event_type 仍支持 fnmatch 通配（task.* 等）与未来新事件，不构成白名单校验。
    """
    from ...runtime.events import EVENT_CATALOG

    return list(EVENT_CATALOG)


@router.post("")
async def create_trigger(body: TriggerCreate, request: fastapi.Request,
                         db: Session = fastapi.Depends(get_db)):
    _validate(db, body)
    record = TriggerRuleRecord(
        name=body.name, tenant_id=body.tenant_id, source=body.source,
        event_type=body.event_type, match=body.match, cron=body.cron,
        secret=encrypt_secret(body.secret),  # M8 静态加密（未配置密钥时明文兼容）
        target_type=body.target_type, target_name=body.target_name,
        input_mode=body.input_mode, template=body.template,
        min_interval_s=body.min_interval_s, enabled=body.enabled)
    db.add(record)
    db.commit()
    audit.record("trigger.create", actor=audit.actor_of(request), target=record.name,
                 detail={"source": body.source, "target": f"{body.target_type}:{body.target_name}"},
                 trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()  # 触发引擎即时生效（事件订阅/cron 重排）
    return _view(record)


@router.patch("/{rule_id}")
async def patch_trigger(rule_id: int, body: TriggerPatch, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    record = db.get(TriggerRuleRecord, rule_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 触发器 {rule_id} 不存在")
    changes = {}
    for field, value in body.model_dump(exclude_unset=True, exclude_none=False).items():
        if value is None and field != "match" and field != "template":
            continue
        if field == "secret":
            value = encrypt_secret(value)  # 更新密钥同样加密落库
        if getattr(record, field, None) != value:
            changes[field] = "updated"
        setattr(record, field, value)
    db.commit()
    audit.record("trigger.update", actor=audit.actor_of(request), target=record.name,
                 detail={"changes": sorted(changes)}, trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()
    return _view(record)


@router.delete("/{rule_id}")
async def delete_trigger(rule_id: int, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    record = db.get(TriggerRuleRecord, rule_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 触发器 {rule_id} 不存在")
    name = record.name
    db.delete(record)
    db.commit()
    audit.record("trigger.delete", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    engine = _engine(request)
    if engine is not None:
        engine.reload()
    return {"name": name, "status": "deleted"}


@router.post("/{rule_id}/test-fire")
async def test_fire(rule_id: int, body: TestFireRequest, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    """手工注入样例 payload 验证触发链路（admin）：走与事件/webhook 完全相同的 fire 通道。"""
    record = db.get(TriggerRuleRecord, rule_id)
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 触发器 {rule_id} 不存在")
    engine = _engine(request)
    if engine is None:
        raise fastapi.HTTPException(status_code=503, detail="EAP-4005 触发引擎未启动")
    result = await engine.fire(snapshot_of(record), "manual", body.data)
    return {"rule": record.name, **result}


# ---------- 入站 webhook（公开端点：签名即凭证） ----------

_seen_event_ids: OrderedDict[str, None] = OrderedDict()  # 进程内重放去重（LRU）
_SEEN_CAP = 4096


@public_router.post("/webhook/{rule_id}")
async def webhook(rule_id: int, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """入站 webhook 触发：校验 X-EAP-Signature（HMAC-SHA256(raw_body, secret) hex）→ 按规则触发。

    - 规则未配置 secret 时放行未签名请求（开发形态；生产建规则应配置密钥）
    - X-EAP-Event-Id 去重：进程内 LRU，重复投递返回 duplicate（多副本为尽力而为）
    """
    record = db.get(TriggerRuleRecord, rule_id)
    if record is None or record.source != "webhook" or not record.enabled:
        raise fastapi.HTTPException(status_code=404,
                                    detail=f"EAP-4004 触发器 {rule_id} 不存在或未启用")
    body = await request.body()
    from ...security_crypto import decrypt_secret

    secret = decrypt_secret(record.secret)
    if secret:
        signature = request.headers.get("X-EAP-Signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            audit.record("trigger.webhook.rejected", actor="webhook", target=record.name,
                         detail={"reason": "signature"},
                         trace_id=getattr(request.state, "trace_id", ""))
            raise fastapi.HTTPException(status_code=401, detail="EAP-3001 webhook 签名无效")
    event_id = request.headers.get("X-EAP-Event-Id", "")
    if event_id:
        if event_id in _seen_event_ids:
            return {"status": "duplicate", "rule": record.name}
        _seen_event_ids[event_id] = None
        while len(_seen_event_ids) > _SEEN_CAP:
            _seen_event_ids.popitem(last=False)
    try:
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            data = {"payload": data}
    except ValueError:
        data = {"body": body.decode("utf-8", "replace")}
    engine = _engine(request)
    if engine is None:
        raise fastapi.HTTPException(status_code=503, detail="EAP-4005 触发引擎未启动")
    result = await engine.fire(snapshot_of(record), "webhook", data)
    return {"status": "accepted", "rule": record.name, "result": result}

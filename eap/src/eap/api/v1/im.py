"""企业 IM 渠道 API（docs/04 §4，M3；M30 任务组 D 深化）：渠道登记 + 三平台回调接入。

回调端点 /api/v1/im/{platform}/{name}/webhook 供 IM 平台服务器调用：
- 飞书：URL 验证（challenge 应答）+ 消息事件（Verification Token 校验）
- 钉钉：加签校验（timestamp+sign 头）+ 回复发到 payload 携带的 sessionWebhook
- 企业微信：GET 验证 URL（echostr 解密回显）+ POST 加密消息（签名+AES 解密）
消息统一路由到渠道绑定的智能体，回复推回群（钉钉发回 sessionWebhook）。
业务处理抛错不抛 5xx（平台要求快速 2xx），转入 im_outbound 重试队列（幂等 + 指数退避）。

M30 新增管理端点（admin + 审计）：
- POST /channels/{name}/credentials：应用级凭据配置（Fernet 加密存储，响应不回显明文）
- POST /channels/{id}/send-card：出站卡片消息（平台无关卡片 → 三平台适配，event_key 幂等）
"""

from __future__ import annotations

import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agents.registry import registry
from ...db import get_db
from ...models import IMChannelRecord
from ...observability import audit
from ...runtime import im as im_rt
from ...runtime import im_outbound as im_out
from ...schemas import InvokeRequest
from ...security_crypto import decrypt_secret, encrypt_secret
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/im", dependencies=[fastapi.Depends(get_db)])

# 管理端点鉴权链：resolve_tenant 先解析凭证，require_api_key 再收紧到 API Key
AUTH_DEP = [fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)]
ADMIN_DEP = [fastapi.Depends(resolve_tenant), fastapi.Depends(require_admin)]


class ChannelCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    platform: str = Field(pattern=r"^(feishu|dingtalk|wecom)$")
    agent: str
    webhook_url: str = Field(default="", max_length=512)
    secret: str | None = Field(default=None, max_length=256)
    app_id: str = Field(default="", max_length=128,
                        description="应用级凭据（M30：飞书 im/v1/messages 应用 API 用）")
    app_secret: str | None = Field(default=None, max_length=256,
                                   description="应用密钥：Fernet 加密存储，响应不回显")
    extra: dict = Field(default_factory=dict)
    note: str = Field(default="", max_length=128)


def _get(db: Session, name: str) -> IMChannelRecord:
    record = db.scalar(select(IMChannelRecord).where(IMChannelRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 IM 渠道 {name} 不存在")
    return record


def _resolve_channel(db: Session, ident: str) -> IMChannelRecord:
    """路径参数兼容渠道 id（数字）与 name（既有管理 API 语义）。"""
    if ident.isdigit():
        record = db.get(IMChannelRecord, int(ident))
        if record is not None:
            return record
    return _get(db, ident)


@router.get("/channels", dependencies=AUTH_DEP)
def list_channels(db: Session = fastapi.Depends(get_db)):
    return [{"name": c.name, "platform": c.platform, "agent": c.agent,
             "enabled": c.enabled, "note": c.note}
            for c in db.scalars(select(IMChannelRecord)).all()]


@router.post("/channels", dependencies=ADMIN_DEP)
def create_channel(body: ChannelCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(IMChannelRecord).where(IMChannelRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 IM 渠道 {body.name} 已存在")
    if body.agent not in registry.names():
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 智能体 {body.agent} 未注册")
    record = IMChannelRecord(name=body.name, platform=body.platform, agent=body.agent,
                             webhook_url=body.webhook_url,
                             secret=encrypt_secret(body.secret) if body.secret else None,
                             app_id=body.app_id,
                             app_secret_enc=encrypt_secret(body.app_secret) if body.app_secret else None,
                             extra=body.extra, note=body.note)
    db.add(record)
    db.commit()
    audit.record("im.channel.create", actor=audit.actor_of(request), target=record.name,
                 detail={"platform": record.platform, "app_id": record.app_id},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "platform": record.platform, "agent": record.agent,
            "app_id": record.app_id,
            "webhook": f"/api/v1/im/{record.platform}/{record.name}/webhook"}


class ChannelCredentials(BaseModel):
    """应用级凭据更新（M30）：app_secret=None 不变更；空串清除；否则 Fernet 加密存储。"""

    app_id: str = Field(default="", max_length=128)
    app_secret: str | None = Field(default=None, max_length=256)


@router.post("/channels/{name}/credentials", dependencies=ADMIN_DEP)
def set_credentials(name: str, body: ChannelCredentials, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    """渠道应用级凭据配置（M30 任务组 D）：secret 复用 M26 Fernet 加密，响应不回显明文。"""
    record = _get(db, name)
    record.app_id = body.app_id
    if body.app_secret is not None:
        record.app_secret_enc = encrypt_secret(body.app_secret) if body.app_secret else None
    db.commit()
    audit.record("im.credentials", actor=audit.actor_of(request), target=name,
                 detail={"app_id": record.app_id, "secret_set": bool(record.app_secret_enc)},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "app_id": record.app_id,
            "app_secret": "***" if record.app_secret_enc else ""}


class CardAction(BaseModel):
    """平台无关卡片按钮：action/tool/args 即 Action 协议（api/v1/actions.py）三要素。"""

    label: str = Field(default="", max_length=64)
    action: str = Field(min_length=1, max_length=64)
    tool: str = Field(default="", max_length=128)
    args: dict = Field(default_factory=dict)
    confirmation: str | None = Field(default=None, max_length=128)


class CardCreate(BaseModel):
    """出站卡片请求体：缺省由 title/text/actions 构造；card 传完整平台无关卡片。"""

    title: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=2048)
    actions: list[CardAction] = Field(default_factory=list)
    event_key: str | None = Field(default=None, max_length=128,
                                  description="幂等键：同渠道同键重复提交仅投递一次；缺省自动生成")
    card: dict | None = Field(default=None,
                              description="完整平台无关卡片 {title, text, actions[]}，优先于下列字段")


@router.post("/channels/{ident}/send-card", dependencies=ADMIN_DEP)
async def send_card(ident: str, body: CardCreate, request: fastapi.Request,
                    db: Session = fastapi.Depends(get_db)):
    """出站卡片消息（M30 任务组 D）：平台无关卡片 → 渠道平台适配发送。

    - 首投在本请求内执行；失败落 pending 重试队列（指数退避，EAP_IM_RETRY_* 可配，超限 dead）
    - 幂等：该渠道已有同 event_key 的 pending/done 投递时跳过不重发（IM 至少一次投递语义）
    """
    record = _resolve_channel(db, ident)
    if not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    card = body.card or {"title": body.title, "text": body.text,
                         "actions": [a.model_dump(exclude_none=True) for a in body.actions]}
    event_key = body.event_key or f"card-{uuid.uuid4().hex}"
    trace_id = getattr(request.state, "trace_id", "")
    if im_out.find_active(db, record.id, event_key) is not None:
        audit.record("im.send_card", actor=audit.actor_of(request), target=record.name,
                     detail={"event_key": event_key, "skipped": True}, trace_id=trace_id)
        return {"event_key": event_key, "skipped": True}
    status, error = "done", ""
    try:
        await im_out.send_card(record, card)
    except Exception as e:  # 首投失败 → pending 入队走后台重试
        status, error = "pending", str(e)[:500]
    im_out.enqueue(db, channel_id=record.id, direction="out", event_key=event_key,
                   payload=card, status=status, attempts=1, error=error)
    db.commit()
    audit.record("im.send_card", actor=audit.actor_of(request), target=record.name,
                 detail={"event_key": event_key, "status": status,
                         "title": str(card.get("title") or "")}, trace_id=trace_id)
    if status == "pending":
        im_out.schedule_retry_loop()
    return {"event_key": event_key, "status": status, "error": error}


@router.post("/channels/{name}/enabled", dependencies=ADMIN_DEP)
def toggle(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = _get(db, name)
    record.enabled = enabled
    db.commit()
    return {"name": record.name, "enabled": record.enabled}


@router.post("/channels/{name}/test", dependencies=ADMIN_DEP)
async def test_push(name: str, db: Session = fastapi.Depends(get_db)):
    """向群机器人 Webhook 推一条测试消息（连通性冒烟）。"""
    record = _get(db, name)
    if not record.webhook_url:
        raise fastapi.HTTPException(status_code=400, detail="EAP-7203 渠道未配置 webhook_url")
    result = await im_rt.post_json(record.webhook_url,
                                   im_rt.format_push(record.platform, "EAP 渠道连通性测试"))
    return {"name": record.name, "result": result}


# ---------- 回调端点（IM 平台服务器调用，无 API Key） ----------

async def _route_to_agent(db: Session, record: IMChannelRecord, text: str,
                          sender: str, reply_url: str | None) -> dict:
    """消息 → 绑定智能体 → 回复推送。失败不抛（IM 回调需快速 2xx）。

    业务处理抛错 → 入重试队列（runtime/im_outbound，direction=in；幂等键按
    平台:渠道:内容派生，平台重复投递同键跳过），由后台循环指数退避重投。
    """
    try:
        resp = await registry.invoke(db, record.agent, InvokeRequest(input=text, user_id=sender or None))
        reply = resp.output
    except Exception as e:
        reply = f"处理失败：{e}"
        im_out.enqueue(db, channel_id=record.id, direction="in",
                       event_key=im_out.derive_event_key(record.platform, record.name, text, sender),
                       payload={"text": text, "sender": sender, "reply_url": reply_url},
                       status="pending", attempts=1, error=str(e)[:500])
        db.commit()  # 回调端点无其他写事务，独立提交保住重试记录
        im_out.schedule_retry_loop()
    if reply_url:  # 钉钉：回发到回调携带的 sessionWebhook
        return await im_rt.post_json(reply_url, im_rt.format_push(record.platform, reply))
    if record.webhook_url:  # 飞书/企业微信：发到渠道配置的群 Webhook
        return await im_rt.post_json(record.webhook_url, im_rt.format_push(record.platform, reply))
    return {"status": 0, "body": "no-webhook"}


@router.post("/feishu/{name}/webhook")
async def feishu_webhook(name: str, payload: dict, request: fastapi.Request,
                         db: Session = fastapi.Depends(get_db)):
    record = _get(db, name)
    if record.platform != "feishu" or not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    challenge = im_rt.feishu_challenge(payload)
    if challenge is not None:
        return challenge
    if not im_rt.feishu_verify_token(payload, decrypt_secret(record.secret)):
        raise fastapi.HTTPException(status_code=401, detail="EAP-7205 Verification Token 校验失败")
    text, sender = im_rt.feishu_parse_message(payload)
    if text:
        await _route_to_agent(db, record, text, sender, None)
    return {"ok": True}  # 官方要求快速 2xx，异步处理由平台侧重试兜底


@router.post("/dingtalk/{name}/webhook")
async def dingtalk_webhook(name: str, payload: dict, request: fastapi.Request,
                           db: Session = fastapi.Depends(get_db)):
    record = _get(db, name)
    if record.platform != "dingtalk" or not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    if record.secret:
        timestamp = request.headers.get("timestamp", "")
        sign = request.headers.get("sign", "")
        if not timestamp or not im_rt.dingtalk_verify(decrypt_secret(record.secret), timestamp, sign):
            raise fastapi.HTTPException(status_code=401, detail="EAP-7205 加签校验失败")
    text, sender, session_webhook = im_rt.parse_inbound("dingtalk", payload)
    if text and session_webhook:
        return await _route_to_agent(db, record, text, sender, session_webhook)
    return {"ok": True}


@router.get("/wecom/{name}/webhook")
async def wecom_verify(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """URL 验证：解密 echostr 并校验签名后，以明文回显。"""
    record = _get(db, name)
    if record.platform != "wecom" or not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    params = request.query_params
    try:
        if not im_rt.wecom_verify(record.extra, params.get("msg_timestamp", ""),
                                  params.get("msg_nonce", ""), params.get("echostr", ""),
                                  params.get("msg_signature", "")):
            raise ValueError("signature mismatch")
        return fastapi.responses.PlainTextResponse(
            im_rt.wecom_decrypt(record.extra, params.get("echostr", "")))
    except Exception as e:
        raise fastapi.HTTPException(status_code=401, detail=f"EAP-7205 企业微信验证失败：{e}") from e


@router.post("/wecom/{name}/webhook")
async def wecom_webhook(name: str, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    """加密消息：验签 → AES 解密 → 智能体 → 推送回复。"""
    record = _get(db, name)
    if record.platform != "wecom" or not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    body = (await request.body()).decode()
    fields = im_rt.wecom_parse_xml(body)
    try:
        if not im_rt.wecom_verify(record.extra, request.query_params.get("msg_timestamp", ""),
                                  request.query_params.get("msg_nonce", ""),
                                  fields.get("encrypt", ""), request.query_params.get("msg_signature", "")):
            raise ValueError("signature mismatch")
        plain_xml = im_rt.wecom_decrypt(record.extra, fields.get("encrypt", ""))
        inner = im_rt.wecom_parse_xml(plain_xml)
    except ValueError as e:
        raise fastapi.HTTPException(status_code=401, detail=f"EAP-7205 {e}") from e
    if inner.get("content"):
        await _route_to_agent(db, record, inner["content"], inner.get("from_user", ""), None)
    return fastapi.responses.PlainTextResponse("success")  # 官方约定应答

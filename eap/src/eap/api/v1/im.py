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

M39-B 新增（docs/18 §二.5 移动远程操作，回调处理内、路由到智能体之前）：
- 卡片按钮点击回调识别保留字 task.approve → 直调任务引擎 approve（远程审批等价
  HITL 决策，落审计 harness.remote.approve），回执「已批准/已拒绝，任务续跑」；
  找不到任务/状态不对 → 回执错误文本（仍 2xx，平台要求快速应答）
- 文本以「/task 」前缀 → /task <agent> <input...> 远程触发：直达任务引擎
  （agent.invoke 异步执行，payload 补 _acl 身份快照），回执「任务已受理 {task_id}」；
  异步完成回执为 M40（远程监控），本版不推
"""

from __future__ import annotations

import base64
import json
import re
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

def _xml_cdata(xml: str, tag: str) -> str:
    """企微回调 XML 的单字段提取（EventKey 等，CDATA 形态同 runtime/im.wecom_parse_xml）。"""
    return next(iter(re.findall(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", xml, re.S)), "")


def _extract_action_ref(platform: str, *, payload: dict | None = None,
                        xml: str = "", query=None) -> dict | None:
    """按钮回调引用还原（im_outbound.build_action_value / build_action_url 的逆）。

    三平台按钮点击到达形态各异，统一还原为 {"action", "channel"?, "args"?}：
    - 飞书：卡片按钮点击事件 payload.action.value（或 event.action.value）为 JSON dict
    - 企业微信：template_card_event 的 EventKey（XML CDATA）为 JSON 串
    - 钉钉：actionURL 点击请求 query 携带 action + base64url(JSON args)
    非按钮回调（普通文本消息等）返回 None。
    """
    raw: object = None
    if platform == "feishu":
        p = payload or {}
        action = p.get("action") or (p.get("event") or {}).get("action") or {}
        raw = action.get("value") if isinstance(action, dict) else None
    elif platform == "wecom":
        try:
            raw = json.loads(_xml_cdata(xml, "EventKey"))
        except ValueError:
            raw = None
    elif platform == "dingtalk" and query is not None:
        action = query.get("action")
        if action:
            ref: dict = {"action": str(action)}
            try:
                b64 = query.get("args") or ""
                ref["args"] = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
                raw = ref
            except (ValueError, json.JSONDecodeError):
                raw = None
    if not isinstance(raw, dict) or not raw.get("action"):
        return None
    return raw


async def _push_receipt(record: IMChannelRecord, reply_url: str | None, text: str) -> dict:
    """回执文本推送：钉钉发回调携带的 sessionWebhook，其余发渠道群 webhook。"""
    url = reply_url or record.webhook_url
    if not url:
        return {"status": 0, "body": "no-webhook"}
    return await im_rt.post_json(url, im_rt.format_push(record.platform, text))


async def _handle_button(db: Session, record: IMChannelRecord, platform: str, *,
                         request: fastapi.Request, payload: dict | None = None,
                         xml: str = "", query=None, sender: str = "",
                         reply_url: str | None = None) -> dict | None:
    """卡片按钮点击回调（路由到智能体之前）：保留字 task.approve → 远程审批决策（M39-B）。

    value 形如 {"action": "task.approve", "channel", "args": {task_id, decision}}——
    调任务引擎 approve（合并决定 → PENDING 续跑）+ 审计 harness.remote.approve
    （docs/18 §二.5：远程审批等价 HITL 决策，detail 标 im 渠道/决策），回执
    「已批准/已拒绝，任务续跑」。找不到任务/状态不对 → 回执错误文本（不 5xx）。
    非该保留字的引用/普通消息返回 None → 走既有链路。
    """
    ref = _extract_action_ref(platform, payload=payload, xml=xml, query=query)
    if ref is None or ref.get("action") != im_out.TASK_APPROVE_ACTION:
        return None
    args = ref.get("args") if isinstance(ref.get("args"), dict) else {}
    task_id = str(args.get("task_id") or "")
    decision = bool(args.get("decision"))
    try:
        state = await request.app.state.task_engine.approve(task_id, decision)
        reply = f"已{'批准' if decision else '拒绝'}，任务续跑（{task_id} → {state}）"
    except KeyError:
        reply = f"审批失败：未找到任务 {task_id or '（空）'}"
    except ValueError as e:
        reply = f"审批失败：{e}"
    audit.record("harness.remote.approve",
                 actor=f"im:{record.platform}/{record.name}/{sender or 'anonymous'}",
                 target=task_id,
                 detail={"channel": record.name, "platform": record.platform,
                         "decision": decision, "via": "im-card"},
                 trace_id=getattr(request.state, "trace_id", ""))
    return await _push_receipt(record, reply_url, reply)


async def _submit_remote_task(db: Session, record: IMChannelRecord, text: str,
                              reply_url: str | None, request: fastapi.Request) -> dict:
    """/task <agent> <input...> 远程触发（M39-B）：IM 消息直达任务引擎异步执行。

    鉴权沿用渠道回调既有安全（三平台签名已验）；payload 补 _acl 身份快照（M36 任务
    通道同法——IM 通道无登录态，如实记 None/空 roles，文档检索走默认可见语义）。
    回执「任务已受理 {task_id}」；异步完成回执为 M40（远程监控），本版不推。
    """
    parts = text[len("/task"):].strip().split(None, 1)
    agent = str(parts[0]) if parts else ""
    task_input = parts[1] if len(parts) > 1 else ""
    if agent not in registry.names():
        return await _push_receipt(record, reply_url,
                                   f"远程触发失败：智能体 {agent or '（空）'} 未注册")
    payload = {"agent": agent, "input": task_input,
               "_acl": {"tenant_id": getattr(request.state, "tenant_id", None),
                        "user_id": getattr(request.state, "user", None),
                        "roles": getattr(request.state, "roles", [])}}
    task_id = await request.app.state.task_engine.submit(db, "agent.invoke", payload)
    return await _push_receipt(record, reply_url, f"任务已受理 {task_id}")


async def _route_to_agent(db: Session, record: IMChannelRecord, text: str,
                          sender: str, reply_url: str | None,
                          request: fastapi.Request | None = None) -> dict:
    """消息 → 绑定智能体 → 回复推送。失败不抛（IM 回调需快速 2xx）。

    「/task 」前缀文本 → 远程触发（M39-B）：直达任务引擎不经智能体对话。
    业务处理抛错 → 入重试队列（runtime/im_outbound，direction=in；幂等键按
    平台:渠道:内容派生，平台重复投递同键跳过），由后台循环指数退避重投。
    """
    if request is not None and text.startswith("/task "):
        return await _submit_remote_task(db, record, text, reply_url, request)
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
    handled = await _handle_button(db, record, "feishu", request=request, payload=payload,
                                   sender=sender, reply_url=None)
    if handled is not None:
        return handled
    if text:
        await _route_to_agent(db, record, text, sender, None, request)
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
    handled = await _handle_button(db, record, "dingtalk", request=request,
                                   query=request.query_params, sender=sender,
                                   reply_url=session_webhook)
    if handled is not None:
        return handled
    if text and session_webhook:
        return await _route_to_agent(db, record, text, sender, session_webhook, request)
    return {"ok": True}


@router.get("/dingtalk/{name}/webhook")
async def dingtalk_action_click(name: str, request: fastapi.Request,
                                db: Session = fastapi.Depends(get_db)):
    """钉钉 actionCard 按钮 actionURL 点击（浏览器 GET 本端点）：识别审批按钮并回执（M39-B）。

    点击请求无钉钉加签头（头由机器人服务器回调携带），鉴权依赖回调引用不可猜测性
    （task_id 为 uuid4 十六进制不可枚举）+ 审计留痕；生产加固（actionURL 一次性
    token）留待 M40 真实联调。
    """
    record = _get(db, name)
    if record.platform != "dingtalk" or not record.enabled:
        raise fastapi.HTTPException(status_code=409, detail="EAP-7204 渠道不可用")
    handled = await _handle_button(db, record, "dingtalk", request=request,
                                   query=request.query_params)
    if handled is None:
        return {"ok": True, "note": "非按钮回调（query 无 action 参数）"}
    return handled


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
    handled = await _handle_button(db, record, "wecom", request=request, xml=plain_xml,
                                   sender=inner.get("from_user", ""))
    if handled is not None:
        return fastapi.responses.PlainTextResponse("success")
    if inner.get("content"):
        await _route_to_agent(db, record, inner["content"], inner.get("from_user", ""), None,
                              request)
    return fastapi.responses.PlainTextResponse("success")  # 官方约定应答

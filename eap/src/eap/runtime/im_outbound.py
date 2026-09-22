"""IM 出站卡片消息适配器 + 投递重试队列（docs/10 遗留，M30 任务组 D）。

## 出站卡片（平台无关 → 三平台适配）
平台无关卡片结构：{"title", "text", "actions": [{"label", "action", "tool", "args",
"confirmation"}]}。actions 与平台 Action 协议（api/v1/actions.py：
ActionInvoke(action, tool, args) → run_action）对齐：按钮点击回调回到既有 IM 回调链路
/api/v1/im/{platform}/{name}/webhook → 路由到绑定智能体 → 由智能体经 run_action
执行业务动作。各平台按钮回调可达构造：
- 飞书 interactive：button.value = build_action_value(...)——点击后飞书 POST 回调事件
  携带 value 到卡片回调地址（即渠道 webhook）；confirmation → button.confirm。
- 钉钉 actionCard：btns[].actionURL = build_action_url(...)——query 携带
  action/tool/args（args 为 base64url(JSON)），点击/回调事件到达 webhook 即可解析执行。
- 企微 template_card：button_list[].key = JSON(build_action_value(...))——点击事件
  （template_card_event）回调携带 key 到 webhook。

## 平台适配形态（应用级 API）
- 飞书：配置 app_id/app_secret 时走应用级 API——POST auth/v3/tenant_access_token/internal
  取租户令牌，再 POST im/v1/messages（msg_type=interactive，content=卡片 JSON 串，
  receive_id/receive_id_type 取渠道 extra）；未配置凭据回退群机器人 webhook 直发卡片。
- 钉钉：群机器人 webhook actionCard；渠道 secret 配置时按官方规则 URL 加签（timestamp+sign）。
- 企微：群机器人 webhook template_card（button_interaction）。
HTTP 发送统一经本模块 post_json（默认 httpx；测试 monkeypatch 注入 fake，不发真实请求）。

## 投递重试队列（进程内 asyncio，无 Redis 依赖）
失败投递（HTTP 非 2xx / 平台业务码非 0 / 回调业务处理抛错）落 im_outbound_logs
（direction=in|out），process_due/retry_loop 按指数退避重投
（base*2^(attempts-1)，封顶 max；EAP_IM_RETRY_MAX_ATTEMPTS 次后置 dead 死信）；
幂等：同 channel+event_key 已 done/pending 的重复投递直接跳过（enqueue 返回 None）。
循环惰性启动（schedule_retry_loop()，由 API 层入队后调用，不改 main.py lifespan），
随事件循环存活，单轮失败不影响循环存活。

## HITL 审批卡片推送（M39-B，docs/18 §二.5 移动远程审批）
任务挂起 WAITING_HUMAN 时（runtime/tasks.py 挂起点）调 notify_hitl：向 enabled 且
extra["notify_hitl"]=true 的渠道推送「审批待办」卡片（agent/工具/task_id 摘要 +
批准/拒绝两按钮）。按钮复用 Action 协议编码，action 取保留字 task.approve、args 携带
task_id + decision；回调端点（api/v1/im.py）识别后直调任务引擎 approve 续跑。
幂等键 hitl:{task_id}；发送失败走既有重试队列。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import IMChannelRecord, IMOutboundLogRecord

log = logging.getLogger("eap.im_outbound")

PLATFORMS = ("feishu", "dingtalk", "wecom")

_SCAN_BATCH = 20  # 单轮扫描的最大重投条数


def _utcnow() -> datetime:
    """库内统一存 naive UTC（与 audit.py 约定一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- 重试参数（EAP_IM_RETRY_*，测试可 monkeypatch 覆盖） ----------

def max_attempts() -> int:
    from ..config import get_settings

    return get_settings().im_retry_max_attempts


def base_delay() -> float:
    from ..config import get_settings

    return get_settings().im_retry_base_seconds


def max_delay() -> float:
    from ..config import get_settings

    return get_settings().im_retry_max_seconds


def poll_seconds() -> float:
    from ..config import get_settings

    return get_settings().im_retry_poll_seconds


def backoff(attempts: int) -> float:
    """指数退避：base * 2^(attempts-1)，封顶 max_delay（秒）。"""
    return min(base_delay() * (2 ** max(0, attempts - 1)), max_delay())


# ---------- HTTP 发送（默认 httpx，测试注入 fake） ----------

async def post_json(url: str, payload: dict, headers: dict | None = None,
                    timeout: float = 10.0) -> dict:
    """发送封装（测试可 monkeypatch）：统一返回 {"status", "body"}。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.post(url, json=payload, headers=headers)
        try:
            body = json.loads(resp.text)
        except (json.JSONDecodeError, TypeError):
            body = resp.text[:500]
        return {"status": resp.status_code, "body": body}


def _check_ok(platform: str, result: dict) -> None:
    """平台业务码校验：非 2xx 或业务码非 0 视为投递失败（调用方入重试队列）。"""
    status = int((result or {}).get("status") or 0)
    if not 200 <= status < 300:
        raise RuntimeError(f"EAP-7208 {platform} 卡片投递失败：HTTP {status}")
    body = (result or {}).get("body")
    if isinstance(body, dict):
        if "code" in body and body["code"] != 0:  # 飞书业务码
            raise RuntimeError(f"EAP-7208 {platform} 卡片投递失败：code={body['code']}")
        if "errcode" in body and body["errcode"] != 0:  # 钉钉/企微业务码
            raise RuntimeError(f"EAP-7208 {platform} 卡片投递失败：errcode={body['errcode']}")


# ---------- 卡片结构：平台无关 → 各平台 payload ----------

def card_callback_path(platform: str, channel_name: str) -> str:
    """按钮回调目标：既有 IM 回调链路端点（api/v1/im.py）→ 路由绑定智能体执行 Action。"""
    return f"/api/v1/im/{platform}/{channel_name}/webhook"


def build_action_value(channel_name: str, action: dict) -> dict:
    """按钮 → Action 协议回调引用（飞书 value / 企微 key 用）。

    语义与 api/v1/actions.py 的 ActionInvoke(action, tool, args) 三要素一致，
    另附 channel 供回调侧定位渠道；回调事件到达 webhook 后由绑定智能体执行。
    """
    ref: dict = {"action": str(action.get("action") or ""), "channel": channel_name}
    if action.get("tool"):
        ref["tool"] = action["tool"]
    if action.get("args"):
        ref["args"] = action["args"]
    return ref


def build_action_url(platform: str, channel_name: str, action: dict,
                     callback_base: str = "") -> str:
    """按钮 → 回调可达 URL（钉钉 actionURL 用）：指向渠道 webhook 端点。

    callback_base 取渠道 extra.callback_base（平台外网可达的 EAP 基址，缺省相对路径）。
    """
    params: dict = {"action": str(action.get("action") or "")}
    if action.get("tool"):
        params["tool"] = action["tool"]
    if action.get("args"):
        raw = json.dumps(action["args"], ensure_ascii=False).encode()
        params["args"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return (callback_base + card_callback_path(platform, channel_name)
            + "?" + urlencode(params))


def _feishu_card(channel_name: str, card: dict) -> dict:
    """飞书 interactive 卡片（open.feishu.cn 卡片协议）。"""
    elements: list = [{"tag": "div", "text": {"tag": "lark_md", "content": str(card.get("text") or "")}}]
    buttons = []
    for a in card.get("actions") or []:
        btn: dict = {"tag": "button", "text": {"tag": "plain_text",
                                               "content": str(a.get("label") or a.get("action") or "")},
                     "type": "primary", "value": build_action_value(channel_name, a)}
        if a.get("confirmation"):
            btn["confirm"] = {"title": {"tag": "plain_text", "content": a["confirmation"]},
                              "text": {"tag": "plain_text", "content": a["confirmation"]}}
        buttons.append(btn)
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": str(card.get("title") or "")},
                       "template": "blue"},
            "elements": elements}


def _dingtalk_card(channel_name: str, platform: str, card: dict, callback_base: str) -> dict:
    """钉钉机器人 actionCard（ocrad 交互形态：btns[].actionURL 回调可达）。"""
    btns = [{"title": str(a.get("label") or a.get("action") or ""),
             "actionURL": build_action_url(platform, channel_name, a, callback_base)}
            for a in card.get("actions") or []]
    return {"msgtype": "actionCard",
            "actionCard": {"title": str(card.get("title") or ""),
                           "text": str(card.get("text") or ""),
                           "btnOrientation": "0", "btns": btns}}


def _wecom_card(channel_name: str, card: dict) -> dict:
    """企微 template_card（button_interaction：button_list[].key 回调可达）。"""
    buttons = [{"text": str(a.get("label") or a.get("action") or ""), "style": "primary",
                "key": json.dumps(build_action_value(channel_name, a), ensure_ascii=False)}
               for a in card.get("actions") or []]
    return {"msgtype": "template_card",
            "template_card": {"card_type": "button_interaction",
                              "source": {"icon_url": "", "desc": "EAP Agent Platform"},
                              "main_title": {"title": str(card.get("title") or "")},
                              "sub_title_text": str(card.get("text") or ""),
                              "button_list": buttons}}


# ---------- 三平台发送适配器 ----------

async def _feishu_token(channel: IMChannelRecord, send) -> str:
    """应用级凭据 → tenant_access_token（im/v1/messages 前置）。"""
    from ..security_crypto import decrypt_secret

    base = (channel.extra or {}).get("feishu_base") or "https://open.feishu.cn"
    res = await send(f"{base}/open-apis/auth/v3/tenant_access_token/internal",
                     {"app_id": channel.app_id, "app_secret": decrypt_secret(channel.app_secret_enc)})
    body = res.get("body")
    if not isinstance(body, dict) or body.get("code") != 0 or not body.get("tenant_access_token"):
        raise RuntimeError(f"EAP-7206 飞书 tenant_access_token 获取失败：{body}")
    return body["tenant_access_token"]


async def _send_feishu(channel: IMChannelRecord, card: dict, send) -> dict:
    extra = channel.extra or {}
    if channel.app_id and channel.app_secret_enc:  # 应用级 API（im/v1/messages）
        receive_id = extra.get("receive_id") or ""
        if not receive_id:
            raise ValueError("EAP-7207 飞书应用级发送需渠道 extra.receive_id")
        token = await _feishu_token(channel, send)
        base = extra.get("feishu_base") or "https://open.feishu.cn"
        url = f"{base}/open-apis/im/v1/messages?receive_id_type={extra.get('receive_id_type') or 'open_id'}"
        payload = {"receive_id": receive_id, "msg_type": "interactive",
                   "content": json.dumps(_feishu_card(channel.name, card), ensure_ascii=False)}
        result = await send(url, payload, headers={"Authorization": f"Bearer {token}"})
        _check_ok("feishu", result)
        return result
    if not channel.webhook_url:  # 回退：群机器人 webhook 直发卡片
        raise ValueError("EAP-7203 渠道未配置 webhook_url 或应用级凭据")
    result = await send(channel.webhook_url,
                        {"msg_type": "interactive", "card": _feishu_card(channel.name, card)})
    _check_ok("feishu", result)
    return result


async def _send_dingtalk(channel: IMChannelRecord, card: dict, send) -> dict:
    if not channel.webhook_url:
        raise ValueError("EAP-7203 渠道未配置 webhook_url")
    url = channel.webhook_url
    if channel.secret:  # 机器人加签（官方规则：timestamp + urlencode(sign)）
        from ..security_crypto import decrypt_secret

        from .im import dingtalk_sign

        ts = str(int(time.time() * 1000))
        sign = dingtalk_sign(decrypt_secret(channel.secret), ts)
        url += ("&" if "?" in url else "?") + f"timestamp={ts}&sign={quote_plus(sign)}"
    callback_base = (channel.extra or {}).get("callback_base") or ""
    payload = _dingtalk_card(channel.name, "dingtalk", card, callback_base)
    result = await send(url, payload)
    _check_ok("dingtalk", result)
    return result


async def _send_wecom(channel: IMChannelRecord, card: dict, send) -> dict:
    if not channel.webhook_url:
        raise ValueError("EAP-7203 渠道未配置 webhook_url")
    result = await send(channel.webhook_url, _wecom_card(channel.name, card))
    _check_ok("wecom", result)
    return result


async def send_card(channel: IMChannelRecord, card: dict, *, sender=None) -> dict:
    """按渠道平台分发卡片消息（应用级 API 形态）；失败抛异常，由调用方入重试队列。

    card 为平台无关结构（见模块 docstring）；sender 可注入 fake 发送函数
    （签名同 post_json，测试用），缺省用本模块 post_json（httpx）。
    """
    platform = (channel.platform or "").lower()
    if platform not in PLATFORMS:
        raise ValueError(f"EAP-7201 未知 IM 平台 {platform}")
    send = sender or post_json
    if platform == "feishu":
        return await _send_feishu(channel, card, send)
    if platform == "dingtalk":
        return await _send_dingtalk(channel, card, send)
    return await _send_wecom(channel, card, send)


# ---------- HITL 审批卡片推送（M39-B，docs/18 §二.5 移动远程审批） ----------

# 按钮编码保留字：action 除业务动作/工具名外新增的保留动作——按钮 value 携带
# {"action": "task.approve", "channel", "args": {task_id, decision}}，回调端点
# （api/v1/im.py）识别后直调任务引擎 approve，不经智能体（远程审批等价 HITL 决策）。
TASK_APPROVE_ACTION = "task.approve"


def build_hitl_card(task_id: str, agent: str, tool: str) -> dict:
    """审批待办卡片（平台无关结构）：批准/拒绝两按钮复用 M30 Action 协议编码。

    按钮 action 取保留字 task.approve，args 携带 task_id + decision；三平台适配
    （飞书 value / 钉钉 actionURL / 企微 key）由既有 _feishu_card/_dingtalk_card/
    _wecom_card 承担，回调引用还原见 api/v1/im._extract_action_ref。
    """
    def _act(decision: bool) -> dict:
        return {"label": "批准" if decision else "拒绝", "action": TASK_APPROVE_ACTION,
                "args": {"task_id": task_id, "decision": decision}}

    return {"title": "审批待办",
            "text": (f"智能体 **{agent or '未知'}** 请求调用工具 **{tool or '未知'}**，"
                     f"任务 `{task_id}` 等待人工审批。"),
            "actions": [_act(True), _act(False)]}


async def notify_hitl(task_id: str, agent: str, tool: str, db: Session, *,
                      sender=None) -> list[str]:
    """HITL 待办 → IM 审批卡片推送（M39-B）：notify_hitl 开关渠道逐个推送。

    - 渠道筛选：enabled 且 extra["notify_hitl"]=true（复用 extra JSON 列，零迁移）
    - 幂等：event_key = hitl:{task_id}——同渠道已有同键 done/pending 投递直接跳过
      （任务重复挂起/重扫不重发）；返回实际处理的渠道名列表（跳过的不含）
    - 失败走既有重试队列：首投失败 enqueue pending（指数退避重投，超限 dead），
      并惰性拉起后台重试循环
    - 传入会话由本函数 commit（挂起点为独立会话、无环境事务）；sender 可注入
      fake 发送函数（签名同 post_json，测试用），缺省本模块 post_json
    """
    channels = db.scalars(select(IMChannelRecord)
                          .where(IMChannelRecord.enabled == True))  # noqa: E712
    card = build_hitl_card(task_id, agent, tool)
    event_key = f"hitl:{task_id}"
    pushed: list[str] = []
    pending = False
    for ch in channels:
        if not (ch.extra or {}).get("notify_hitl"):
            continue
        if find_active(db, ch.id, event_key) is not None:
            continue  # 幂等：该渠道已推送过本任务的审批卡片
        status, error = "done", ""
        try:
            await send_card(ch, card, sender=sender)
        except Exception as e:  # 首投失败 → pending 入队走后台重试
            status, error = "pending", str(e)[:500]
            pending = True
        enqueue(db, channel_id=ch.id, direction="out", event_key=event_key,
                payload=card, status=status, attempts=1, error=error)
        pushed.append(ch.name)
    db.commit()
    if pending:
        schedule_retry_loop()
    return pushed


# ---------- 投递日志 / 重试队列 ----------

def derive_event_key(platform: str, channel_name: str, *parts) -> str:
    """确定性幂等键（平台侧重投/重复回调去重）：平台:渠道:sha256(规范拼接)。"""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]
    return f"{platform}:{channel_name}:{digest}"


def find_active(db: Session, channel_id: int, event_key: str) -> IMOutboundLogRecord | None:
    """同 channel+event_key 的在途/已成功投递（幂等消费依据）。"""
    return db.scalar(select(IMOutboundLogRecord).where(
        IMOutboundLogRecord.channel_id == channel_id,
        IMOutboundLogRecord.event_key == event_key,
        IMOutboundLogRecord.status.in_(("pending", "done"))))


def enqueue(db: Session, *, channel_id: int, direction: str, event_key: str, payload: dict,
            status: str = "pending", attempts: int = 1, error: str = "",
            next_retry_at: datetime | None = None) -> IMOutboundLogRecord | None:
    """投递入队（幂等）：同 channel+event_key 已 done/pending 则返回 None 跳过。

    - status="done"：首投即成功，仅落投递日志（next_retry_at 为空）
    - status="pending"：首投失败（attempts=已试次数），next_retry_at 缺省按退避排定；
      显式传参用于测试注入时刻。复用请求级事务（flush 不 commit），调用方 commit。
    """
    if find_active(db, channel_id, event_key) is not None:
        return None
    if next_retry_at is None and status == "pending":
        next_retry_at = _utcnow() + timedelta(seconds=backoff(max(1, attempts)))
    rec = IMOutboundLogRecord(channel_id=channel_id, direction=direction, event_key=event_key,
                              payload=payload, status=status, attempts=attempts,
                              error=(error or "")[:500], next_retry_at=next_retry_at)
    db.add(rec)
    db.flush()
    return rec


async def _redeliver_inbound(db: Session, channel: IMChannelRecord, payload: dict) -> None:
    """入站回调重投：文本重发绑定智能体 → 回复推送（同 api/v1/im._route_to_agent 语义）。"""
    from ..agents.registry import registry
    from ..schemas import InvokeRequest

    from . import im as im_rt

    resp = await registry.invoke(db, channel.agent,
                                 InvokeRequest(input=str(payload.get("text") or ""),
                                               user_id=str(payload.get("sender") or "") or None))
    reply_url = payload.get("reply_url") or channel.webhook_url
    if reply_url:
        await im_rt.post_json(reply_url, im_rt.format_push(channel.platform, resp.output))


async def _attempt(db: Session, rec: IMOutboundLogRecord) -> None:
    """单条投递尝试：成功 → done（清错误）；失败 → 退避重试，超上限 → dead。"""
    channel = db.get(IMChannelRecord, rec.channel_id)
    rec.attempts += 1
    try:
        if channel is None:
            raise RuntimeError(f"EAP-4004 渠道 {rec.channel_id} 不存在")
        if rec.direction == "out":
            await send_card(channel, rec.payload or {})
        else:
            await _redeliver_inbound(db, channel, rec.payload or {})
        rec.status = "done"
        rec.error = ""
    except Exception as e:  # 投递失败是队列常态，不随异常中断
        rec.error = str(e)[:500]
        if rec.attempts >= max_attempts():
            rec.status = "dead"
            log.warning("IM 投递死信 id=%s key=%s：%s", rec.id, rec.event_key, rec.error)
        else:
            rec.status = "pending"
            rec.next_retry_at = _utcnow() + timedelta(seconds=backoff(rec.attempts))


async def process_due() -> int:
    """扫描并尝试全部到期投递（pending 且 next_retry_at<=now）；返回处理条数。

    测试可直接调用驱动一轮（免等待后台循环）；重试循环每轮亦经此函数。
    """
    now = _utcnow()
    processed = 0
    with SessionLocal() as db:
        recs = db.scalars(select(IMOutboundLogRecord)
                          .where(IMOutboundLogRecord.status == "pending",
                                 IMOutboundLogRecord.next_retry_at <= now)
                          .order_by(IMOutboundLogRecord.next_retry_at)
                          .limit(_SCAN_BATCH)).all()
        for rec in recs:
            await _attempt(db, rec)
            processed += 1
        db.commit()
    return processed


# ---------- 重试循环（惰性启动，随事件循环存活） ----------

_task: asyncio.Task | None = None
_wakeup: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def schedule_retry_loop() -> None:
    """确保重试循环在当前事件循环运行（惰性启动；已在跑则仅唤醒提前扫描）。

    由 API 层在入队 pending 投递后调用（不改 main.py lifespan；无事件循环时跳过，
    交由首个异步调用方启动）。
    """
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
            log.warning("IM 重试轮询异常: %s", e)
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=poll_seconds())
        except TimeoutError:
            pass
        _wakeup.clear()

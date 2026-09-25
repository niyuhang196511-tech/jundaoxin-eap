"""企业 IM 运行时（docs/04 §4，M3）：飞书 / 钉钉 / 企业微信 机器人接入。

三平台统一两种流：
- 推送（出站）：群机器人 Webhook，各平台 payload 格式不同，此处统一封装
- 接入（入站）：回调端点收到用户消息 → 路由到绑定智能体 → 回复推回群
  - 飞书：Verification Token 校验 + URL challenge 应答
  - 钉钉：HMAC-SHA256 时间戳加签校验，回复发回 payload 携带的 sessionWebhook
  - 企业微信：SHA1 签名 + AES-256-CBC 消息解密（微信自研封装，needs cryptography）

M55-C 新增（docs/10 遗留「通讯录/群管理应用 API」）：通讯录/群列表代理查询——
平台按渠道登记的应用级凭据（app_id/app_secret）代发对应平台的通讯录 API，
响应统一为平台无关形态；真实平台联调（真实凭证）为 L3 外部条件，测试经
fake 注入离线验证（模式同 test_im 的 post_json monkeypatch）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import re
import struct

PLATFORMS = ("feishu", "dingtalk", "wecom")


# ---------- 出站：群机器人 Webhook 推送 ----------

def format_push(platform: str, text: str) -> dict:
    if platform == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if platform == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if platform == "wecom":
        return {"msgtype": "text", "text": {"content": text}}
    raise ValueError(f"EAP-7201 未知 IM 平台 {platform}")


async def post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    """推送封装（测试可 monkeypatch）。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.post(url, json=payload)
        return {"status": resp.status_code, "body": _safe_json(resp.text)}


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:500]


# ---------- 入站：飞书 ----------

def feishu_challenge(payload: dict) -> dict | None:
    """URL 验证：官方要求原样回传 challenge。非验证消息返回 None。"""
    if isinstance(payload, dict) and "challenge" in payload:
        return {"challenge": payload["challenge"]}
    return None


def feishu_verify_token(payload: dict, secret: str | None) -> bool:
    if not secret:
        return True  # 未配置 token 则不校验（开发模式）
    header = (payload or {}).get("header") or {}
    return header.get("token") == secret


def feishu_parse_message(payload: dict) -> tuple[str, str]:
    """(text, sender_id)。仅处理 im.message.receive_v1 文本消息。"""
    event = (payload or {}).get("event") or {}
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return "", ""
    try:
        content = json.loads(message.get("content") or "{}")
    except json.JSONDecodeError:
        return "", ""
    sender = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id", "")
    return str(content.get("text") or "").strip(), sender


# ---------- 入站：钉钉 ----------

def dingtalk_sign(secret: str, timestamp: str) -> str:
    """官方规则：sign = base64(HMAC-SHA256(secret, f"{timestamp}\\n{secret}"))。"""
    raw = hmac_mod.new(secret.encode(), f"{timestamp}\n{secret}".encode(),
                       hashlib.sha256).digest()
    return base64.b64encode(raw).decode()


def dingtalk_verify(secret: str, timestamp: str, sign: str) -> bool:
    return hmac_mod.compare_digest(dingtalk_sign(secret, timestamp), sign or "")


def dingtalk_parse_message(payload: dict) -> tuple[str, str, str | None]:
    """(text, sender_id, sessionWebhook)。回复必须发到回调携带的 sessionWebhook。"""
    text = (payload or {}).get("text") or {}
    if isinstance(text, dict):
        text = text.get("content", "")
    return (str(text or "").strip(), str((payload or {}).get("senderStaffId") or ""),
            (payload or {}).get("sessionWebhook"))


# ---------- 入站：企业微信（自研 AES 封装） ----------
#
# 注意：以下 SHA1 签名与 AES-256-CBC 是企业微信官方回调协议的强制规范（互操作要求，
# 无法与平台侧协商更换强算法），并非本项目的安全选型；升级需等微信官方变更协议。

def _wecom_key(extra: dict) -> bytes:
    key_b64 = (extra or {}).get("aes_key") or ""
    if len(key_b64) != 43:
        raise ValueError("EAP-7202 企业微信 EncodingAESKey 须为 43 位字符串")
    return base64.b64decode(key_b64 + "=")


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def wecom_verify(extra: dict, timestamp: str, nonce: str, encrypt: str,
                 msg_signature: str) -> bool:
    token = (extra or {}).get("token") or ""
    return hmac_mod.compare_digest(wecom_signature(token, timestamp, nonce, encrypt),
                                   msg_signature or "")


def wecom_decrypt(extra: dict, encrypt_b64: str) -> str:
    """解密官方封装：AES-256-CBC(key=EncodingAESKey+ '=', iv=key[:16])，
    明文 = random(16) + msg_len(4, 网络序) + msg + receiveid。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _wecom_key(extra)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    d = cipher.decryptor()
    plain = d.update(base64.b64decode(encrypt_b64)) + d.finalize()
    msg_len = struct.unpack(">I", plain[16:20])[0]
    return plain[20:20 + msg_len].decode()


def wecom_encrypt(extra: dict, plaintext_msg: str, receive_id: str = "") -> str:
    """测试/自测用：按官方封装加密。明文 = random(16) + msg_len(4) + msg + receiveid。"""
    import os

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _wecom_key(extra)
    msg = plaintext_msg.encode()
    data = os.urandom(16) + struct.pack(">I", len(msg)) + msg + receive_id.encode()
    pad = 32 - len(data) % 32
    data += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    e = cipher.encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()


def wecom_parse_xml(xml: str) -> dict:
    """从回调 XML 提取字段（官方回调为 XML；只取开发用到的纯文本字段，防注入面最小化）。"""
    def _tag(name: str) -> str:
        m = re.search(rf"<{name}><!\[CDATA\[(.*?)\]\]></{name}>", xml, re.S)
        return m.group(1) if m else ""
    return {"encrypt": _tag("Encrypt"), "from_user": _tag("FromUserName"),
            "content": _tag("Content")}


# ---------- 入站公共：消息解析 ----------

def parse_inbound(platform: str, payload: dict) -> tuple[str, str, str | None]:
    """统一解析为 (text, sender, reply_url)。reply_url 仅钉钉回包使用。"""
    if platform == "feishu":
        text, sender = feishu_parse_message(payload)
        return text, sender, None
    if platform == "dingtalk":
        return dingtalk_parse_message(payload)
    return "", "", None


# ---------- 通讯录 / 群管理代理查询（M55-C，docs/10 遗留「通讯录/群管理应用 API」） ----------
#
# 形态：渠道代理查询——按渠道登记的应用级凭据（app_id/app_secret，Fernet 解密）代发
# 对应平台通讯录/群列表 API，响应统一为平台无关形态（API 层落审计 im.directory.query）。
# 平台覆盖（与 PLATFORMS 一致，实现按各家公开 API 形态构造；真实平台联调=L3 外部条件）：
# - 飞书：POST /open-apis/auth/v3/tenant_access_token/internal 取租户令牌 →
#   GET /open-apis/contact/v3/users/find_by_department（用户）、GET /open-apis/im/v1/chats（群）
# - 钉钉：GET /gettoken → POST /topapi/v2/user/list?access_token=（用户，dept_id 必填缺省根部门 1）、
#   GET /chat/list?access_token=（群）
# - 企微：GET /cgi-bin/gettoken（corpid=app_id，corpsecret=app_secret）→
#   GET /cgi-bin/user/list（用户，fetch_child=1 含子部门）、GET /cgi-bin/appchat/list（群）
# 分页差异统一为不透明 page_token（飞书 page_token / 钉钉 next_cursor / 企微 next_cursor）；
# keyword 为页内过滤（三平台列表 API 均无服务端关键字参数，如实按页过滤）。

_DIRECTORY_MAX_PAGE = 100  # 单页上限（三平台通讯录 API 页大小统一钳制）


async def get_json(url: str, params: dict | None = None, headers: dict | None = None,
                   timeout: float = 10.0) -> dict:
    """GET 封装（测试可 monkeypatch）：统一返回 {"status", "body"}。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.get(url, params=params, headers=headers)
        return {"status": resp.status_code, "body": _safe_json(resp.text)}


def _directory_credentials(channel) -> tuple[str, str]:
    """渠道应用级凭据校验：返回 (app_id, 解密 app_secret)；缺一即 ValueError（API 层转 400）。

    channel 为 IMChannelRecord（鸭子类型：platform/app_id/app_secret_enc/extra，避免本
    纯函数模块反向依赖 models）。
    """
    from ..security_crypto import decrypt_secret

    app_id = getattr(channel, "app_id", "") or ""
    enc = getattr(channel, "app_secret_enc", None)
    if not app_id or not enc:
        raise ValueError("EAP-7203 渠道未配置应用级凭据（app_id/app_secret），通讯录代理查询需先配置")
    return app_id, decrypt_secret(enc)


def _directory_ok(platform: str, result: dict) -> dict:
    """上游结果校验 + 业务体提取：HTTP 非 2xx / 业务码非 0 → RuntimeError（API 层转 502）。

    错误摘要只取 HTTP 状态码与平台 msg/errmsg 字段，不含 URL 与任何凭据——钉钉/企微
    的 access_token 在 query 上，绝不入错误文本（502 透传「脱敏摘要」纪律）。
    """
    status = int((result or {}).get("status") or 0)
    body = (result or {}).get("body")
    if not 200 <= status < 300:
        raise RuntimeError(f"EAP-7208 {platform} 通讯录上游调用失败：HTTP {status}")
    if not isinstance(body, dict):
        raise RuntimeError(f"EAP-7208 {platform} 通讯录上游调用失败：响应非 JSON 形态")
    code = body.get("code")
    errcode = body.get("errcode")
    bad = code if code not in (0, None) else (errcode if errcode not in (0, None) else 0)
    if bad:
        msg = str(body.get("msg") or body.get("errmsg") or body.get("error_message") or "")[:200]
        raise RuntimeError(f"EAP-7208 {platform} 通讯录上游调用失败：code={bad} {msg}")
    return body


async def _directory_token(channel, app_id: str, app_secret: str) -> str:
    """按平台取应用级 access token（每次现取不缓存——联调期最简正确形态）。"""
    platform = channel.platform
    extra = channel.extra or {}
    if platform == "feishu":
        base = extra.get("feishu_base") or "https://open.feishu.cn"
        body = _directory_ok("feishu", await post_json(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret}))
        tok = body.get("tenant_access_token")
    elif platform == "dingtalk":
        base = extra.get("dingtalk_base") or "https://oapi.dingtalk.com"
        body = _directory_ok("dingtalk", await get_json(
            f"{base}/gettoken", params={"appkey": app_id, "appsecret": app_secret}))
        tok = body.get("access_token")
    else:  # wecom：corpid=app_id、corpsecret=app_secret
        base = extra.get("wecom_base") or "https://qyapi.weixin.qq.com"
        body = _directory_ok("wecom", await get_json(
            f"{base}/cgi-bin/gettoken", params={"corpid": app_id, "corpsecret": app_secret}))
        tok = body.get("access_token")
    if not tok:
        raise RuntimeError(f"EAP-7206 {platform} 应用 access_token 获取失败（凭据或权限）")
    return str(tok)


def _norm_user(raw: dict) -> dict:
    """平台用户 → 平台无关形态 {user_id, name, department_ids}；email/mobile 缺失如实省略。

    平台字段差异：飞书 open_id/user_id + department_ids；钉钉 userid + dept_id_list；
    企微 userid + department。
    """
    out = {"user_id": str(raw.get("user_id") or raw.get("userid") or raw.get("open_id") or ""),
           "name": str(raw.get("name") or ""),
           "department_ids": [str(d) for d in
                              (raw.get("department_ids") or raw.get("dept_id_list")
                               or raw.get("department") or []) if d is not None]}
    for key in ("email", "mobile"):
        val = raw.get(key)
        if val:
            out[key] = str(val)
    return out


def _norm_chat(raw: dict) -> dict:
    """平台群 → 平台无关形态 {chat_id, name}（飞书 chat_id/name；钉钉 chatid/title；
    企微 chatid/name）。"""
    return {"chat_id": str(raw.get("chat_id") or raw.get("chatid") or ""),
            "name": str(raw.get("name") or raw.get("title") or "")}


def _keyword_filter(items: list[dict], keyword: str) -> list[dict]:
    """keyword 页内过滤：匹配 name/user_id/email/mobile 任一字段子串（不区分大小写）。"""
    k = keyword.strip().lower()
    if not k:
        return items
    return [u for u in items
            if any(k in str(u.get(f) or "").lower()
                   for f in ("name", "user_id", "email", "mobile"))]


def _cursor_int(page_token: str) -> int:
    """钉钉/企微数字游标解析：非法 token 报 ValueError（API 层转 400），不静默归零
    （静默归零会让调用方翻页永远停在第一页）。"""
    try:
        return int(page_token) if page_token else 0
    except ValueError as e:
        raise ValueError("EAP-7203 翻页 token 须为数字游标（本平台分页形态）") from e


async def directory_users(channel, *, department_id: str = "", keyword: str = "",
                          page_size: int = 50, page_token: str = "") -> dict:
    """通讯录用户代理查询（M55-C）：按渠道凭据代发平台通讯录用户列表 API。

    返回平台无关形态 {"items": [{user_id, name, department_ids, email?, mobile?}],
    "next_page_token"}（next_page_token 为不透明翻页游标，空串=无下一页）。
    参数差异如实处理：飞书 department_id 可选；钉钉/企微缺省根部门（dept_id=1 /
    department_id=1，企微 fetch_child=1 含子部门）；page_size 统一钳制 [1,100]；
    keyword 为页内过滤。失败抛 ValueError（API 层转 400）/ RuntimeError（转 502）。
    真实平台联调=L3 外部条件（本实现按各家公开 API 形态构造，测试经 fake 离线验证）。
    """
    platform = (channel.platform or "").lower()
    if platform not in PLATFORMS:
        raise ValueError(f"EAP-7201 平台 {platform} 暂不支持通讯录代理查询（支持：{', '.join(PLATFORMS)}）")
    app_id, app_secret = _directory_credentials(channel)
    extra = channel.extra or {}
    size = min(max(int(page_size), 1), _DIRECTORY_MAX_PAGE)
    if platform == "feishu":
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("feishu_base") or "https://open.feishu.cn"
        params: dict = {"page_size": size, "user_id_type": "user_id"}
        if department_id:
            params["department_id"] = department_id
        if page_token:
            params["page_token"] = page_token
        body = _directory_ok("feishu", await get_json(
            f"{base}/open-apis/contact/v3/users/find_by_department", params=params,
            headers={"Authorization": f"Bearer {token}"}))
        data = body.get("data") or {}
        items = [_norm_user(u) for u in (data.get("items") or []) if isinstance(u, dict)]
        nxt = str(data.get("page_token") or "") if data.get("has_more") else ""
    elif platform == "dingtalk":
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("dingtalk_base") or "https://oapi.dingtalk.com"
        try:
            dept_id = int(department_id) if department_id else 1  # 钉钉 dept_id 必填，缺省根部门 1
        except ValueError as e:
            raise ValueError("EAP-7203 钉钉通讯录 department_id 须为数字部门 ID") from e
        payload = {"dept_id": dept_id, "cursor": _cursor_int(page_token), "size": size}
        body = _directory_ok("dingtalk", await post_json(
            f"{base}/topapi/v2/user/list?access_token={token}", payload))
        result = body.get("result") or {}
        items = [_norm_user(u) for u in (result.get("list") or []) if isinstance(u, dict)]
        nxt = str(result.get("next_cursor") or "") if result.get("has_more") else ""
    else:  # wecom（token 在 query，无需 header）
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("wecom_base") or "https://qyapi.weixin.qq.com"
        params = {"access_token": token, "department_id": department_id or "1",
                  "fetch_child": "1", "limit": size}
        if page_token:
            params["cursor"] = page_token
        body = _directory_ok("wecom", await get_json(f"{base}/cgi-bin/user/list", params=params))
        items = [_norm_user(u) for u in (body.get("userlist") or []) if isinstance(u, dict)]
        nxt = str(body.get("next_cursor") or "")
    return {"items": _keyword_filter(items, keyword), "next_page_token": nxt}


async def directory_chats(channel, *, page_size: int = 50, page_token: str = "") -> dict:
    """群列表代理查询（M55-C）：按渠道凭据代发平台群列表 API，形态同 directory_users。

    平台语义差异如实处理：飞书 im/v1/chats=机器人所在群；钉钉 chat/list=机器人可
    发消息的群；企微 appchat/list=应用创建的群（企微无「全量群列表」API，范围以
    官方口径为准）。返回 {"items": [{chat_id, name}], "next_page_token"}。
    真实平台联调=L3 外部条件。
    """
    platform = (channel.platform or "").lower()
    if platform not in PLATFORMS:
        raise ValueError(f"EAP-7201 平台 {platform} 暂不支持群列表代理查询（支持：{', '.join(PLATFORMS)}）")
    app_id, app_secret = _directory_credentials(channel)
    extra = channel.extra or {}
    size = min(max(int(page_size), 1), _DIRECTORY_MAX_PAGE)
    if platform == "feishu":
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("feishu_base") or "https://open.feishu.cn"
        params: dict = {"page_size": size}
        if page_token:
            params["page_token"] = page_token
        body = _directory_ok("feishu", await get_json(
            f"{base}/open-apis/im/v1/chats", params=params,
            headers={"Authorization": f"Bearer {token}"}))
        data = body.get("data") or {}
        items = [_norm_chat(u) for u in (data.get("items") or []) if isinstance(u, dict)]
        nxt = str(data.get("page_token") or "") if data.get("has_more") else ""
    elif platform == "dingtalk":
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("dingtalk_base") or "https://oapi.dingtalk.com"
        body = _directory_ok("dingtalk", await get_json(
            f"{base}/chat/list",
            params={"access_token": token, "cursor": _cursor_int(page_token), "size": size}))
        items = [_norm_chat(u) for u in (body.get("results") or []) if isinstance(u, dict)]
        nxt = str(body.get("next_cursor") or "") if body.get("next_cursor") else ""
    else:  # wecom
        token = await _directory_token(channel, app_id, app_secret)
        base = extra.get("wecom_base") or "https://qyapi.weixin.qq.com"
        params = {"access_token": token, "limit": size}
        if page_token:
            params["cursor"] = page_token
        body = _directory_ok("wecom", await get_json(f"{base}/cgi-bin/appchat/list", params=params))
        items = [_norm_chat(u) for u in (body.get("chatlist") or []) if isinstance(u, dict)]
        nxt = str(body.get("next_cursor") or "")
    return {"items": items, "next_page_token": nxt}

"""企业 IM 运行时（docs/04 §4，M3）：飞书 / 钉钉 / 企业微信 机器人接入。

三平台统一两种流：
- 推送（出站）：群机器人 Webhook，各平台 payload 格式不同，此处统一封装
- 接入（入站）：回调端点收到用户消息 → 路由到绑定智能体 → 回复推回群
  - 飞书：Verification Token 校验 + URL challenge 应答
  - 钉钉：HMAC-SHA256 时间戳加签校验，回复发回 payload 携带的 sessionWebhook
  - 企业微信：SHA1 签名 + AES-256-CBC 消息解密（微信自研封装，needs cryptography）
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

"""对称加密：LLM 供应商 Key 等静态秘密的静态存储加密（M8 安全加固）。

- 密钥：EAP_SECRET_KEY（32 字节 base64 或任意口令 → sha256 派生）；未配置 = 明文存储 +
  启动警告（兼容开发形态，不阻断）
- 算法：Fernet（AES-128-CBC + HMAC，cryptography 库）；密文带版本前缀 `enc1:` 便于轮换
- 接口：encrypt_secret / decrypt_secret / is_encrypted

连接器（ConnectorRecord）与模型（ModelRecord）的 api_key 走本模块。
"""

from __future__ import annotations

import base64
import hashlib

_ENC_PREFIX = "enc1:"


def _fernet():
    from cryptography.fernet import Fernet

    secret = _secret_key()
    if not secret:
        return None
    # 口令 → 定长 32 字节 → base64（Fernet 要求 URL-safe 32 字节）
    derived = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _secret_key() -> str | None:
    from .config import get_settings

    return get_settings().secret_key or None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_ENC_PREFIX)


def encrypt_secret(plain: str | None) -> str | None:
    """明文 → `enc1:<fernet>`；未配置 EAP_SECRET_KEY 时原样返回（明文兼容，启动有警告）。"""
    if not plain:
        return plain
    f = _fernet()
    if f is None:
        return plain
    if is_encrypted(plain):
        return plain  # 已加密（幂等）
    return _ENC_PREFIX + f.encrypt(plain.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    """`enc1:<fernet>` → 明文；非密文（明文兼容期/未配密钥）原样返回。失败返回 None。"""
    if not stored:
        return stored
    if not is_encrypted(stored):
        return stored
    f = _fernet()
    if f is None:
        return None  # 库里是密文但密钥未配置：无法解密
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        return None

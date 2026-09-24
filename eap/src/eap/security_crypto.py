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


def _secret_key() -> str | None:
    from .config import get_settings

    return get_settings().secret_key or None


def _previous_keys() -> list[str]:
    """轮换前的旧密钥列表（M48-C 密钥轮换）：EAP_SECRET_KEY_PREVIOUS，逗号分隔。

    轮换流程：EAP_SECRET_KEY 换新值 + EAP_SECRET_KEY_PREVIOUS=旧值 → 新写入用新密钥，
    存量密文用旧密钥仍可解（decrypt_secret 自动回退）；存量全量重加密跑
    scripts/reencrypt_secrets.py（完成后可移除 PREVIOUS）。
    """
    from .config import get_settings

    raw = get_settings().secret_key_previous or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _fernets() -> list[tuple[str, object]]:
    """当前密钥在前、旧密钥在后（顺序即解密尝试顺序）；未配置任何密钥返回空。"""
    from cryptography.fernet import Fernet

    keys = []
    current = _secret_key()
    if current:
        keys.append(current)
    keys.extend(k for k in _previous_keys() if k != current)
    out = []
    for secret in keys:
        derived = hashlib.sha256(secret.encode()).digest()
        out.append((secret, Fernet(base64.urlsafe_b64encode(derived))))
    return out


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_ENC_PREFIX)


def encrypt_secret(plain: str | None) -> str | None:
    """明文 → `enc1:<fernet>`；未配置 EAP_SECRET_KEY 时原样返回（明文兼容，启动有警告）。

    恒用当前密钥（EAP_SECRET_KEY）加密——轮换期旧密文由 decrypt_secret 回退解出，
    写回时自然转为新密文。
    """
    if not plain:
        return plain
    fernets = _fernets()
    if not fernets:
        return plain
    if is_encrypted(plain):
        return plain  # 已加密（幂等）
    return _ENC_PREFIX + fernets[0][1].encrypt(plain.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    """`enc1:<fernet>` → 明文；非密文（明文兼容期/未配密钥）原样返回。失败返回 None。

    密钥轮换（M48-C）：先试当前密钥，失败逐个回退 EAP_SECRET_KEY_PREVIOUS 旧密钥——
    全部失败才返回 None（调用方按解密失败语义处理）。
    """
    if not stored:
        return stored
    if not is_encrypted(stored):
        return stored
    fernets = _fernets()
    if not fernets:
        return None  # 库里是密文但密钥未配置：无法解密
    token = stored[len(_ENC_PREFIX):].encode()
    for _secret, f in fernets:
        try:
            return f.decrypt(token).decode()
        except Exception:
            continue
    return None

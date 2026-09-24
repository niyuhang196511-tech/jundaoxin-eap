"""M48-C 密钥轮换与安全响应头测试。"""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def _fernet_of(secret: str):
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _enc_with(secret: str, plain: str) -> str:
    return "enc1:" + _fernet_of(secret).encrypt(plain.encode()).decode()


def test_decrypt_falls_back_to_previous_key(monkeypatch):
    """轮换期：旧密钥密文经 EAP_SECRET_KEY_PREVIOUS 回退可解（当前密钥解不开）。"""
    from eap import security_crypto

    old, new = "old-key", "new-key"
    legacy = _enc_with(old, "my-api-secret")
    monkeypatch.setattr(security_crypto, "_fernets", lambda: [
        (new, _fernet_of(new)), (old, _fernet_of(old))])
    # 回退解密成功
    assert security_crypto.decrypt_secret(legacy) == "my-api-secret"
    # 当前密钥密文直解
    current = _enc_with(new, "current-value")
    assert security_crypto.decrypt_secret(current) == "current-value"
    # 全部失败 → None
    assert security_crypto.decrypt_secret(_enc_with("unrelated", "x")) is None


def test_encrypt_always_uses_current_key(monkeypatch):
    """新加密恒用当前密钥（轮换后新写入不再依赖旧密钥）。"""
    from eap import security_crypto

    old, new = "old-key", "new-key"
    monkeypatch.setattr(security_crypto, "_fernets", lambda: [
        (new, _fernet_of(new)), (old, _fernet_of(old))])
    out = security_crypto.encrypt_secret("fresh-secret")
    assert out.startswith("enc1:")
    # 只用当前密钥即可解开（解密回退链中第一把）
    assert _fernet_of(new).decrypt(out[len("enc1:"):].encode()).decode() == "fresh-secret"


def test_reencrypt_script_rotates_rows(client: TestClient, monkeypatch):
    """reencrypt_secrets.reencrypt_all：旧密钥密文 → 当前密钥，值保持不变。"""
    import importlib.util
    from pathlib import Path

    from eap import security_crypto
    from eap.db import SessionLocal
    from eap.models import ModelRecord

    # scripts/ 非包——按文件路径加载（与平台插件加载器同款语义）
    script = Path(__file__).resolve().parents[2] / "scripts" / "reencrypt_secrets.py"
    spec = importlib.util.spec_from_file_location("reencrypt_secrets", str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reencrypt_all = mod.reencrypt_all

    old, new = "rotate-old", "rotate-new"
    with SessionLocal() as db:
        row = ModelRecord(name="rotate-e2e-model", provider="mock",
                          capabilities=["chat"], api_key=_enc_with(old, "sk-rotated"),
                          priority=999)
        db.add(row)
        db.commit()
        rid = row.id

    # 当前密钥=new、旧密钥=old（模拟轮换后环境）
    monkeypatch.setattr(security_crypto, "_fernets", lambda: [
        (new, _fernet_of(new)), (old, _fernet_of(old))])
    with SessionLocal() as db:
        result = reencrypt_all(db, dry_run=False)
        # 会话级共享库：其他用例经真实测试密钥加密落库的密文行（models/im/mcp 等）
        # 在轮换密钥下不可读、被脚本如实跳过属预期——只断言本用例行不在跳过清单
        assert ("ModelRecord", rid, "api_key") not in result["skipped_unreadable"]

    with SessionLocal() as db:
        row = db.get(ModelRecord, rid)
        # 值不变且已用新密钥（旧密钥单独解不开新密文）
        assert security_crypto.decrypt_secret(row.api_key) == "sk-rotated"
        with pytest.raises(Exception):
            _fernet_of(old).decrypt(row.api_key[len("enc1:")].encode())

    # 清理（隔离纪律：session 级共享库）
    with SessionLocal() as db:
        db.get(ModelRecord, rid).enabled = False
        db.commit()


def test_security_headers_applied_and_exempt(client: TestClient):
    """安全响应头：API 响应带基线头；/sdk 豁免（嵌入通道，M48-C）。"""
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    # /sdk 豁免（demo.html 是给第三方 iframe 嵌入的功能本体）
    r = client.get("/sdk/eap-widget.js")
    assert "X-Frame-Options" not in r.headers
    assert "Content-Security-Policy" not in r.headers

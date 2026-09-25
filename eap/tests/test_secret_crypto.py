"""M8 静态秘密加密测试：Fernet 加解密 + 模型/连接器存储加密 + 未配密钥兼容。

M52-D 可重入：模型/连接器名 uuid 唯一化——脏库重跑不撞唯一约束（409 会击穿
assert 200）；连接器的 tool_name 同步唯一化，防残留启用连接器的同名工具干扰
resolve_tool 命中序。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_crypto_roundtrip(monkeypatch):
    """encrypt/decrypt 往返 + 幂等 + 密钥缺失语义。"""
    from eap import security_crypto
    from eap.config import get_settings

    monkeypatch.setenv("EAP_SECRET_KEY", "unit-test-secret")
    get_settings.cache_clear()
    try:
        enc = security_crypto.encrypt_secret("sk-abc123")
        assert enc.startswith("enc1:") and enc != "sk-abc123"
        assert security_crypto.decrypt_secret(enc) == "sk-abc123"
        assert security_crypto.encrypt_secret(enc) == enc  # 幂等
        assert security_crypto.decrypt_secret("sk-plain") == "sk-plain"  # 明文兼容期原样
        assert security_crypto.encrypt_secret(None) is None
    finally:
        get_settings.cache_clear()


def test_crypto_without_key_stores_plain(monkeypatch):
    """未配置 EAP_SECRET_KEY：明文兼容（启动警告在 __main__），不阻断。"""
    from eap import security_crypto
    from eap.config import get_settings

    monkeypatch.delenv("EAP_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert security_crypto.encrypt_secret("sk-x") == "sk-x"
        assert security_crypto.decrypt_secret("sk-x") == "sk-x"
        assert security_crypto.decrypt_secret("enc1:garbage") is None  # 密文+无密钥 → None
    finally:
        get_settings.cache_clear()


def test_model_api_key_encrypted_at_rest(client: TestClient, monkeypatch):
    """注册模型 → 库内密文（enc1: 前缀）→ provider 调用拿到解密明文。"""
    from eap import security_crypto
    from eap.config import get_settings
    from eap.db import SessionLocal
    from eap.models import ModelRecord

    model = f"enc-test-model-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("EAP_SECRET_KEY", "unit-test-secret")
    get_settings.cache_clear()
    try:
        resp = client.post("/api/v1/models", headers=AUTH, json={
            "name": model, "capabilities": ["chat"],
            "provider": "openai_compat", "base_url": "https://api.example.com/v1",
            "api_key": (live_key := "sk-unit-" + uuid.uuid4().hex[:12]), "priority": 90,
        })
        assert resp.status_code == 200, resp.text

        with SessionLocal() as db:
            record = db.query(ModelRecord).filter_by(name=model).one()
            assert record.api_key.startswith("enc1:")
            assert security_crypto.decrypt_secret(record.api_key) == live_key
        # 列表接口不回显 key
        listed = client.get("/api/v1/models", headers=AUTH).json()
        row = next(m for m in listed if m["name"] == model)
        assert "api_key" not in row
    finally:
        get_settings.cache_clear()
        # M52-D 二波收尾：priority=90 的启用态 openai_compat 残留模型会被后续所有
        # 能力路由用例排进降级链先真实出站（api.example.com 超时 ~13s/次）再落
        # mock——用毕禁用（对齐 test_eval_gate/test_hub 收尾纪律）
        client.post(f"/api/v1/models/{model}/enabled?enabled=false", headers=AUTH)


def test_connector_api_key_encrypted_at_rest(client: TestClient, monkeypatch):
    """连接器同理：库内密文，验证端点用解密值出站。"""
    from eap import security_crypto
    from eap.config import get_settings
    from eap.db import SessionLocal
    from eap.models import ConnectorRecord

    conn = f"enc-conn-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("EAP_SECRET_KEY", "unit-test-secret")
    get_settings.cache_clear()
    try:
        resp = client.post("/api/v1/connectors", headers=AUTH, json={
            "name": conn, "kind": "rest", "base_url": "https://erp.example.com/api",
            "api_key": (conn_key := "conn-unit-" + uuid.uuid4().hex[:12]),
            "endpoints": [{"name": "ping", "tool_name": f"erp-{uuid.uuid4().hex[:8]}.ping",
                           "method": "GET", "path": "/"}],
        })
        assert resp.status_code == 200, resp.text

        with SessionLocal() as db:
            record = db.query(ConnectorRecord).filter_by(name=conn).one()
            assert record.api_key.startswith("enc1:")
            assert security_crypto.decrypt_secret(record.api_key) == conn_key
    finally:
        get_settings.cache_clear()

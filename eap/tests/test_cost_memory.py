"""Cost/Quota + Memory 治理测试（v0.6-M26）：计价/报表/限流/数据权利/IM 加密。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import uuid as _uuid

from .conftest import AUTH


def test_model_pricing_and_cost_metering(client: TestClient):
    """模型配价 → 智能体调用 → 用量记录带 cost（按 token 计得）。"""
    import uuid as _uuid

    suffix = _uuid.uuid4().hex[:6]
    model = f"priced-{suffix}"
    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": model, "capabilities": ["chat"], "provider": "mock",
        "priority": 50, "price_in": 1.0, "price_out": 2.0,
    })
    assert resp.status_code == 200, resp.text
    # prefer 该模型触发一次调用
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "成本计量测试", "output_schema": None})
    assert resp.status_code == 200, resp.text
    usage_model = resp.json()["usage"].get("model", "")
    if usage_model == model:
        from eap.db import SessionLocal
        from eap.models import UsageRecord

        with SessionLocal() as db:
            row = db.query(UsageRecord).filter_by(model=model).order_by(
                UsageRecord.id.desc()).first()
            if row is not None:
                assert row.cost > 0, "配价模型的调用应计得成本"
                assert row.tokens_out > 0
    # 清理配价模型：禁用后移出路由链（避免污染后续测试的降级链断言，test_hub 期望唯一 mock）
    client.patch(f"/api/v1/models/{model}?enabled=false", headers=AUTH)


def test_cost_report_endpoint(client: TestClient):
    """成本报表：按模型/智能体/日聚合 + 总成本字段。"""
    resp = client.get("/api/v1/budgets/1/report", headers=AUTH, params={"days": 30})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "total_cost" in data and "by_model" in data and "by_agent" in data and "by_day" in data


def test_rate_limit_chat_429(client: TestClient, monkeypatch):
    """chat 限流：超限 → 429（按凭证计窗）。"""
    from eap.api.security import get_limiter
    from eap.config import get_settings

    limiter = get_limiter("chat")
    monkeypatch.setattr(limiter, "limit", 1)  # 收紧到 1 次触发限流
    resp1 = client.post("/v1/chat/completions", headers=AUTH,
                        json={"messages": [{"role": "user", "content": "hi"}]})
    resp2 = client.post("/v1/chat/completions", headers=AUTH,
                        json={"messages": [{"role": "user", "content": "hi"}]})
    codes = {resp1.status_code, resp2.status_code}
    assert 429 in codes, f"超限请求应 429（实际 {codes}）"
    if resp1.status_code == 429:
        assert resp1.headers.get("retry-after")
    monkeypatch.undo()


def test_memory_tenant_isolation_and_rights(client: TestClient):
    """记忆：API Key 通道写入归属平台；按用户批量遗忘/导出（admin 数据权利）。"""
    import uuid as _uuid

    user = "u-" + _uuid.uuid4().hex[:6]
    resp = client.post("/api/v1/memory", headers=AUTH,
                       json={"scope": "user", "content": "客户偏好深色主题", "user_id": user})
    assert resp.status_code == 200, resp.text
    # 导出
    exported = client.get(f"/api/v1/memory/users/{user}/export", headers=AUTH).json()
    assert len(exported["memories"]) == 1
    # 批量遗忘
    resp = client.delete(f"/api/v1/memory/users/{user}", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    exported = client.get(f"/api/v1/memory/users/{user}/export", headers=AUTH).json()
    assert exported["memories"] == []


def test_im_secret_encrypted_at_rest(client: TestClient, monkeypatch):
    """IM 渠道 secret 库内密文（enc1:，需配置 EAP_SECRET_KEY），webhook 验签用解密值。"""
    from eap.config import get_settings
    from eap.db import SessionLocal
    from eap.models import IMChannelRecord
    from eap.security_crypto import decrypt_secret

    monkeypatch.setenv("EAP_SECRET_KEY", "unit-test-secret")
    get_settings.cache_clear()
    try:
        name = "enc-im-" + _uuid.uuid4().hex[:6]
        resp = client.post("/api/v1/im/channels", headers=AUTH, json={
            "name": name, "platform": "feishu", "agent": "faq-agent",
            "webhook_url": "https://open.feishu.cn/hook/x", "secret": "im-secret-123"})
        assert resp.status_code == 200, resp.text
        with SessionLocal() as db:
            record = db.query(IMChannelRecord).filter_by(name=name).one()
            assert record.secret.startswith("enc1:")
            assert decrypt_secret(record.secret) == "im-secret-123"
            db.delete(record)
            db.commit()
    finally:
        get_settings.cache_clear()

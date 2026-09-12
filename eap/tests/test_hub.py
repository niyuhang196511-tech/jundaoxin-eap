"""模型中心测试：能力路由、降级链、定制模型注册。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_chain_capability_routing(client: TestClient):
    from eap.db import SessionLocal
    from eap.modelhub.router import hub

    with SessionLocal() as db:
        chain = hub.chain_for(db, capability="chat")
        assert chain, "至少应有 mock-llm"
        assert all("chat" in m.capabilities for m in chain)
        # priority 升序
        prios = [m.priority for m in chain]
        assert prios == sorted(prios)


def test_fallback_to_mock(client: TestClient):
    """注册一个必然失败的 openai_compat 高优先级模型 → 降级链应落到 mock。"""
    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": "dead-model",
        "capabilities": ["chat"],
        "provider": "openai_compat",
        "base_url": "http://127.0.0.1:9",  # 不可路由
        "api_key": "x",
        "remote_model": "x",
        "priority": 1,
    })
    assert resp.status_code == 200, resp.text

    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "messages": [{"role": "user", "content": "降级链测试"}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "mock-llm", "应降级到 mock"
    assert "降级链测试" in data["choices"][0]["message"]["content"]


def test_register_model_validation(client: TestClient):
    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": "bad-caps", "capabilities": ["telepathy"],
    })
    assert resp.status_code == 422

    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": "dup-mock-llm", "capabilities": ["chat"],
    })
    assert resp.status_code == 200

"""模型中心测试：能力路由、降级链、定制模型注册。

M52-D 可重入：模型名 uname 唯一化（脏库重跑不撞唯一约束）；priority=1 的必败
高优模型用完即停用——残留启用会抢链头，污染后续文件的模型路由/优先级断言。
"""

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


def test_fallback_to_mock(client: TestClient, uname):
    """注册一个必然失败的 openai_compat 高优先级模型 → 降级链应落到 mock。"""
    name = uname("dead-model")
    try:
        resp = client.post("/api/v1/models", headers=AUTH, json={
            "name": name,
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
    finally:
        # 隔离纪律：用后停用，priority=1 残留会抢后续所有 chat 调用的链头
        client.patch(f"/api/v1/models/{name}?enabled=false", headers=AUTH)


def test_register_model_validation(client: TestClient, uname):
    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": "bad-caps", "capabilities": ["telepathy"],
    })
    assert resp.status_code == 422

    resp = client.post("/api/v1/models", headers=AUTH, json={
        "name": uname("dup-mock-llm"), "capabilities": ["chat"],
    })
    assert resp.status_code == 200

"""注册钩子 SDK 测试：注册/幂等/冲突/manifest 校验/发现/调用。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def test_builtin_agent_registered(client: TestClient):
    resp = client.get("/api/v1/agents", headers=AUTH)
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "faq-agent" in names
    agent = next(a for a in resp.json() if a["name"] == "faq-agent")
    assert agent["status"] == "started"
    assert agent["source"] == "builtin"


def test_faq_agent_invoke_with_citations(client: TestClient):
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agent"] == "faq-agent"
    assert data["agent_version"] == "1.1.0"
    assert data["invocation_id"]
    assert data["trace_id"]
    assert data["citations"], "客服回答应带引用"
    assert data["citations"][0]["kb"] == "website-faq"


def test_agent_card(client: TestClient):
    resp = client.get("/api/v1/agents/faq-agent/card", headers=AUTH)
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "faq-agent"
    assert card["endpoint"] == "/api/v1/agents/faq-agent/invocations"


def test_unknown_agent_404(client: TestClient):
    resp = client.post("/api/v1/agents/no-such/invocations", headers=AUTH,
                       json={"input": "hi"})
    assert resp.status_code == 404


def test_register_idempotent_and_conflict():
    from eap.agents.app import AgentApp
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import registry

    class Dummy(AgentApp):
        async def on_invoke(self, request):
            raise NotImplementedError

    m = AgentManifest(name="dummy-x", version="1.0.0")
    registry.register(Dummy, m)
    registry.register(Dummy, m)  # 同类同版本幂等

    class Another(AgentApp):
        async def on_invoke(self, request):
            raise NotImplementedError

    with pytest.raises(ValueError, match="已注册"):
        registry.register(Another, AgentManifest(name="dummy-x", version="1.0.1"))


def test_manifest_validation():
    from pydantic import ValidationError

    from eap.agents.manifest import AgentManifest

    with pytest.raises(ValidationError):
        AgentManifest(name="Bad Name", version="1.0.0")  # 大写/空格
    with pytest.raises(ValidationError):
        AgentManifest(name="ok-name", version="1.0")  # 非 semver
    with pytest.raises(ValidationError):
        AgentManifest(name="ok-name", version="1.0.0", kind="Wrong")  # kind 校验

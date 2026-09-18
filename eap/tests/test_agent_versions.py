"""Agent 配置版本层测试（v0.5-①）：版本管线 / 配置校验 / 覆盖层生效。"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from .conftest import AUTH


def _create(client: TestClient, agent: str, version: str, config: dict | None = None, notes: str = ""):
    return client.post(f"/api/v1/agents/{agent}/versions", headers=AUTH,
                       json={"version": version, "config": config or {}, "notes": notes})


def test_version_pipeline(client: TestClient):
    # draft → publish：指针生效，目录可见
    assert _create(client, "faq-agent", "1.1.0", {"system_prompt": "版本角色"}).status_code == 200
    resp = client.post("/api/v1/agents/faq-agent/versions/1.1.0/publish", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["published_version"] == "1.1.0"
    listing = client.get("/api/v1/agents", headers=AUTH).json()
    assert next(a for a in listing if a["name"] == "faq-agent")["published_version"] == "1.1.0"

    # 非 draft 不可修改
    resp = client.patch("/api/v1/agents/faq-agent/versions/1.1.0", headers=AUTH,
                        json={"config": {"system_prompt": "x"}})
    assert resp.status_code == 400

    # 第二版发布 → 旧版自动归档
    assert _create(client, "faq-agent", "1.2.0", {"system_prompt": "新版本角色"}).status_code == 200
    assert client.post("/api/v1/agents/faq-agent/versions/1.2.0/publish", headers=AUTH).status_code == 200
    versions = client.get("/api/v1/agents/faq-agent/versions", headers=AUTH).json()
    states = {v["version"]: v["state"] for v in versions["versions"]}
    assert states["1.1.0"] == "archived"
    assert states["1.2.0"] == "published"

    # 回滚 → 最近归档版重发布
    resp = client.post("/api/v1/agents/faq-agent/rollback", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["published_version"] == "1.1.0"

    # 版本差异
    resp = client.get("/api/v1/agents/faq-agent/versions/1.1.0/diff/1.2.0", headers=AUTH)
    assert resp.status_code == 200
    assert "system_prompt" in {c["key"] for c in resp.json()["changes"]}


def test_deprecate_keeps_serving(client: TestClient):
    """deprecated 为下线过渡态：指针保留、覆盖层仍生效。"""
    assert _create(client, "faq-agent", "1.4.0", {"system_prompt": "过渡角色"}).status_code == 200
    assert client.post("/api/v1/agents/faq-agent/versions/1.4.0/publish", headers=AUTH).status_code == 200
    resp = client.post("/api/v1/agents/faq-agent/versions/1.4.0/deprecate", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["state"] == "deprecated"
    versions = client.get("/api/v1/agents/faq-agent/versions", headers=AUTH).json()
    assert versions["published_version"] == "1.4.0"


def test_config_validation_and_conflict(client: TestClient):
    resp = _create(client, "faq-agent", "9.9.9", {"no_such_key": 1})
    assert resp.status_code == 400  # 白名单外键
    resp = _create(client, "faq-agent", "9.9.9", {"output_schema": {"type": "not-a-type"}})
    assert resp.status_code == 400  # 非法 JSON Schema
    resp = _create(client, "faq-agent", "9.9.9", {"temperature": 9})
    assert resp.status_code == 400  # 区间
    resp = _create(client, "faq-agent", "9.9.9", {"tools": ["", 1]})
    assert resp.status_code == 400
    assert _create(client, "faq-agent", "9.9.9", {"temperature": 0.5}).status_code == 200
    assert _create(client, "faq-agent", "9.9.9", {}).status_code == 409  # 重复版本


# ---------- 覆盖层生效（集成） ----------

from eap.agents.app import AgentApp  # noqa: E402
from eap.agents.manifest import AgentManifest  # noqa: E402
from eap.agents.registry import registry  # noqa: E402
from eap.agents.sdk import register_agent  # noqa: E402
from eap.schemas import InvokeRequest, InvokeResult  # noqa: E402


@register_agent(AgentManifest(name="overlay-dummy-agent", version="1.0.0"), source="sdk")
class OverlayDummy(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        with self.ctx.db() as db:
            completion = await self.ctx.chat(db, messages=[{"role": "user", "content": request.input}])
            record = completion.record
            return InvokeResult(content=self.ctx.system_role("code-default"),
                                usage={"model": record.name})


@register_agent(AgentManifest(name="overlay-tools-agent", version="1.0.0"), source="sdk")
class OverlayTools(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        from eap.runtime.tools import Tool

        async def _handler(args: str) -> str:
            return json.dumps({"ok": True})

        tools = [
            Tool(name="tool_alpha", description="a",
                 parameters={"type": "object", "properties": {}}, handler=_handler),
            Tool(name="tool_beta", description="b",
                 parameters={"type": "object", "properties": {}}, handler=_handler),
        ]
        with self.ctx.db() as db:
            rr = await self.ctx.run_loop(db, [{"role": "user", "content": request.input}],
                                         system="s", tools=tools)
            return InvokeResult(content=rr.content, steps=rr.steps, usage=rr.usage)


def _start(name: str) -> None:
    asyncio.run(registry.start_agent(name))


def test_overlay_system_and_model_prefer(client: TestClient):
    _start("overlay-dummy-agent")
    # 发布前：代码默认行为
    resp = client.post("/api/v1/agents/overlay-dummy-agent/invocations", headers=AUTH,
                       json={"input": "hi"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["output"] == "code-default"
    assert resp.json()["config_version"] is None

    # 第二个 mock 模型（低优先级）：prefer 覆盖时被置顶路由
    client.post("/api/v1/models", headers=AUTH,
                json={"name": "mock-llm-b", "capabilities": ["chat"], "provider": "mock",
                      "priority": 200})
    cfg = {"system_prompt": "版本角色提示词", "model_prefer": "mock-llm-b"}
    assert _create(client, "overlay-dummy-agent", "1.0.0", cfg).status_code == 200
    assert client.post("/api/v1/agents/overlay-dummy-agent/versions/1.0.0/publish",
                       headers=AUTH).status_code == 200

    resp = client.post("/api/v1/agents/overlay-dummy-agent/invocations", headers=AUTH,
                       json={"input": "hi"})
    data = resp.json()
    assert data["output"] == "版本角色提示词"
    assert data["usage"]["model"] == "mock-llm-b"
    assert data["config_version"] == "1.0.0"


def test_overlay_tools_whitelist(client: TestClient):
    """overlay.tools 白名单过滤工具清单：mock 取过滤后清单的第一个工具发起调用。"""
    _start("overlay-tools-agent")
    resp = client.post("/api/v1/agents/overlay-tools-agent/invocations", headers=AUTH,
                       json={"input": "hi"})
    assert resp.status_code == 200, resp.text
    assert any("tool_alpha" in s for s in resp.json()["steps"])

    assert _create(client, "overlay-tools-agent", "1.0.0",
                   {"tools": ["tool_beta"]}).status_code == 200
    assert client.post("/api/v1/agents/overlay-tools-agent/versions/1.0.0/publish",
                       headers=AUTH).status_code == 200
    resp = client.post("/api/v1/agents/overlay-tools-agent/invocations", headers=AUTH,
                       json={"input": "hi"})
    steps = resp.json()["steps"]
    assert any("tool_beta" in s for s in steps)
    assert not any("tool_alpha" in s for s in steps)


def test_stream_result_carries_config_version(client: TestClient):
    """SSE result 帧携带命中的配置版本（流式路径覆盖层同语义）。"""
    assert _create(client, "faq-agent", "1.3.0", {"system_prompt": "流式角色"}).status_code == 200
    assert client.post("/api/v1/agents/faq-agent/versions/1.3.0/publish", headers=AUTH).status_code == 200
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？", "stream": True})
    assert resp.status_code == 200
    result_frames = [json.loads(block.split("data: ", 1)[1])
                     for block in resp.text.split("\n\n")
                     if block.startswith("event: result")]
    assert result_frames and result_frames[0]["config_version"] == "1.3.0"


def test_cleanup_faq_overlay(client: TestClient):
    """发布空配置归零 faq-agent 覆盖层，避免影响其他测试文件的既有行为断言。"""
    assert _create(client, "faq-agent", "8.8.8", {}).status_code == 200
    assert client.post("/api/v1/agents/faq-agent/versions/8.8.8/publish", headers=AUTH).status_code == 200

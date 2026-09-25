"""Agent 配置版本层测试（v0.5-①）：版本管线 / 配置校验 / 覆盖层生效。

M52-D 可重入：faq-agent 版本号唯一化（pattern ^\\d+\\.\\d+\\.\\d+$——第三段取随机数，
脏库重跑不撞 (agent, version) 唯一约束，回滚断言依赖 updated_at 最新归档版不受残留
干扰）；本模块专属 overlay-* 智能体在模块开始前清空版本层（点状差量清理），保证
「发布前=纯代码默认行为（config_version None）」断言在脏库上同样成立。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from .conftest import AUTH


def _ver(base: str) -> str:
    """合规且跨运行唯一的版本号：f"{base}.{随机第三段}"（base 形如 "1.1"）。"""
    return f"{base}.{uuid.uuid4().int % 10**8}"


@pytest.fixture(scope="module", autouse=True)
def _reset_overlay_agents(client: TestClient):
    """overlay-dummy-agent / overlay-tools-agent 仅本模块使用：模块开始前删除其
    配置版本行并复位发布指针——上一遍运行发布的覆盖层若残留，「发布前代码默认
    行为」断言（output=code-default / config_version None / tool_alpha 可用）必炸。"""
    from eap.db import SessionLocal
    from eap.models import AgentRecord, AgentVersionRecord

    with SessionLocal() as db:
        for agent in ("overlay-dummy-agent", "overlay-tools-agent"):
            for row in db.scalars(select(AgentVersionRecord)
                                  .where(AgentVersionRecord.agent_name == agent)).all():
                db.delete(row)
            rec = db.scalar(select(AgentRecord).where(AgentRecord.name == agent))
            if rec is not None:
                rec.published_version = None
        db.commit()
    yield


def _create(client: TestClient, agent: str, version: str, config: dict | None = None, notes: str = ""):
    return client.post(f"/api/v1/agents/{agent}/versions", headers=AUTH,
                       json={"version": version, "config": config or {}, "notes": notes})


def test_version_pipeline(client: TestClient):
    v11, v12 = _ver("1.1"), _ver("1.2")
    # draft → publish：指针生效，目录可见
    assert _create(client, "faq-agent", v11, {"system_prompt": "版本角色"}).status_code == 200
    resp = client.post(f"/api/v1/agents/faq-agent/versions/{v11}/publish", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["published_version"] == v11
    listing = client.get("/api/v1/agents", headers=AUTH).json()
    assert next(a for a in listing if a["name"] == "faq-agent")["published_version"] == v11

    # 非 draft 不可修改
    resp = client.patch(f"/api/v1/agents/faq-agent/versions/{v11}", headers=AUTH,
                        json={"config": {"system_prompt": "x"}})
    assert resp.status_code == 400

    # 第二版发布 → 旧版自动归档
    assert _create(client, "faq-agent", v12, {"system_prompt": "新版本角色"}).status_code == 200
    assert client.post(f"/api/v1/agents/faq-agent/versions/{v12}/publish", headers=AUTH).status_code == 200
    versions = client.get("/api/v1/agents/faq-agent/versions", headers=AUTH).json()
    states = {v["version"]: v["state"] for v in versions["versions"]}
    assert states[v11] == "archived"
    assert states[v12] == "published"

    # 回滚 → 最近归档版重发布（updated_at 最新的归档版 = v11，脏库残留归档版更早不干扰）
    resp = client.post("/api/v1/agents/faq-agent/rollback", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["published_version"] == v11

    # 版本差异
    resp = client.get(f"/api/v1/agents/faq-agent/versions/{v11}/diff/{v12}", headers=AUTH)
    assert resp.status_code == 200
    assert "system_prompt" in {c["key"] for c in resp.json()["changes"]}


def test_deprecate_keeps_serving(client: TestClient):
    """deprecated 为下线过渡态：指针保留、覆盖层仍生效。"""
    v14 = _ver("1.4")
    assert _create(client, "faq-agent", v14, {"system_prompt": "过渡角色"}).status_code == 200
    assert client.post(f"/api/v1/agents/faq-agent/versions/{v14}/publish", headers=AUTH).status_code == 200
    resp = client.post(f"/api/v1/agents/faq-agent/versions/{v14}/deprecate", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["state"] == "deprecated"
    versions = client.get("/api/v1/agents/faq-agent/versions", headers=AUTH).json()
    assert versions["published_version"] == v14


def test_config_validation_and_conflict(client: TestClient):
    v999 = _ver("9.9")
    resp = _create(client, "faq-agent", v999, {"no_such_key": 1})
    assert resp.status_code == 400  # 白名单外键
    resp = _create(client, "faq-agent", v999, {"output_schema": {"type": "not-a-type"}})
    assert resp.status_code == 400  # 非法 JSON Schema
    resp = _create(client, "faq-agent", v999, {"temperature": 9})
    assert resp.status_code == 400  # 区间
    resp = _create(client, "faq-agent", v999, {"tools": ["", 1]})
    assert resp.status_code == 400
    assert _create(client, "faq-agent", v999, {"temperature": 0.5}).status_code == 200
    assert _create(client, "faq-agent", v999, {}).status_code == 409  # 重复版本（同一唯一号重复提交）


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
    v13 = _ver("1.3")
    assert _create(client, "faq-agent", v13, {"system_prompt": "流式角色"}).status_code == 200
    assert client.post(f"/api/v1/agents/faq-agent/versions/{v13}/publish", headers=AUTH).status_code == 200
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？", "stream": True})
    assert resp.status_code == 200
    result_frames = [json.loads(block.split("data: ", 1)[1])
                     for block in resp.text.split("\n\n")
                     if block.startswith("event: result")]
    assert result_frames and result_frames[0]["config_version"] == v13


def test_cleanup_faq_overlay(client: TestClient):
    """发布空配置归零 faq-agent 覆盖层，避免影响其他测试文件的既有行为断言。"""
    v888 = _ver("8.8")
    assert _create(client, "faq-agent", v888, {}).status_code == 200
    assert client.post(f"/api/v1/agents/faq-agent/versions/{v888}/publish", headers=AUTH).status_code == 200

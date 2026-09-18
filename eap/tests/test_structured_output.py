"""结构化输出测试（v0.5-②）：mock 合成合规 / 解析校验 / 重试回退 / API 集成。"""

from __future__ import annotations

import json

import jsonschema
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "回答内容"},
        "confidence": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer", "enum": [1, 2, 3]},
    },
    "required": ["answer", "confidence"],
}


def test_mock_synthesis_conforms_to_schema():
    """mock 合成器对嵌套 schema 产出合法 JSON（离线确定性合同）。"""
    from eap.modelhub.providers import synthesize_from_schema

    data = synthesize_from_schema(SCHEMA)
    jsonschema.validate(data, SCHEMA)  # 不抛即合规
    assert data["answer"].startswith("mock-")
    assert data["priority"] == 1  # enum 取首项
    assert len(data["tags"]) == 1


def test_parse_structured_variants():
    """解析：裸 JSON / 围栏包裹 / 带前后 prose / 非法输出。"""
    from eap.modelhub.router import parse_structured

    data, err = parse_structured('{"answer": "ok", "confidence": 0.9}', SCHEMA)
    assert err == "" and data["answer"] == "ok"
    data, err = parse_structured('```json\n{"answer": "ok", "confidence": 0.9}\n```', SCHEMA)
    assert err == "" and data is not None
    data, err = parse_structured('前置说明 {"answer": "ok", "confidence": 0.9} 后置说明', SCHEMA)
    assert err == "" and data is not None
    data, err = parse_structured("纯文本回复，没有 JSON", SCHEMA)
    assert data is None and "JSON" in err
    data, err = parse_structured('{"confidence": 1.0}', SCHEMA)
    assert data is None  # 缺 required answer → 校验失败


def test_hub_complete_with_schema_retries(client):
    """mock 输出始终合规 → data 直接就位；天然非法场景由 parse 校验兜底（回退纯文本）。"""
    import asyncio

    from eap.db import SessionLocal
    from eap.modelhub.router import hub
    from eap.models import ModelRecord

    async def _run():
        with SessionLocal() as db:
            record = db.query(ModelRecord).filter_by(name="mock-llm").first()
            completion = await hub.complete(
                db, [{"role": "user", "content": "hi"}],
                response_schema=SCHEMA,
            )
            assert record is not None
            return completion

    completion = asyncio.run(_run())
    assert completion.result.data is not None
    jsonschema.validate(completion.result.data, SCHEMA)
    assert completion.result.content.startswith("{")


def test_invoke_with_output_schema(client: TestClient):
    """请求级 output_schema：InvokeResponse.data 携带校验通过的 JSON。"""
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "如何创建知识库？", "output_schema": SCHEMA})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["data"] is not None
    jsonschema.validate(data["data"], SCHEMA)
    assert data["data_schema"] == SCHEMA


def test_agent_overlay_schema_via_config_version(client: TestClient):
    """配置版本 output_schema：请求未带 schema 时生效（发布即结构化）。"""
    cfg = {"output_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }}
    assert client.post("/api/v1/agents/faq-agent/versions", headers=AUTH,
                       json={"version": "7.7.7", "config": cfg}).status_code == 200
    assert client.post("/api/v1/agents/faq-agent/versions/7.7.7/publish", headers=AUTH).status_code == 200
    resp = client.post("/api/v1/agents/faq-agent/invocations", headers=AUTH,
                       json={"input": "介绍一下平台"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["data"] is not None and "summary" in data["data"]


def test_run_loop_structuring_with_tools(client: TestClient):
    """带工具 + schema：最终回答后结构化收尾（RunResult.data）。"""
    import asyncio
    import json as _json

    from eap.agents.app import AgentApp
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import registry
    from eap.agents.sdk import register_agent
    from eap.runtime.tools import Tool
    from eap.schemas import InvokeRequest, InvokeResult

    @register_agent(AgentManifest(name="struct-tools-agent", version="1.0.0"), source="sdk")
    class StructTools(AgentApp):
        async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
            async def _handler(args: str) -> str:
                return _json.dumps({"stock": 12})

            tool = Tool(name="stock.query", description="库存查询",
                        parameters={"type": "object", "properties": {}}, handler=_handler)
            with self.ctx.db() as db:
                rr = await self.ctx.run_loop(
                    db, [{"role": "user", "content": request.input}], system="s",
                    tools=[tool], response_schema={
                        "type": "object",
                        "properties": {"stock": {"type": "integer"}},
                        "required": ["stock"],
                    })
                return InvokeResult(content=rr.content, data=rr.data, data_schema=rr.data_schema)

    asyncio.run(registry.start_agent("struct-tools-agent"))
    resp = client.post("/api/v1/agents/struct-tools-agent/invocations", headers=AUTH,
                       json={"input": "查库存"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["data"] is not None and data["data"]["stock"] == 1  # mock 合成 integer → 1

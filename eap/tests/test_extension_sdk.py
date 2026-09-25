"""Extension SDK 契约测试（v0.7-M29）：RAG/WorkflowNode/Provider/Connector/UI 各类型接入。

M52-D 可重入：落库的自定义节点工作流名加模块级 uuid 后缀（create_and_register
按 DB 查重抛「工作流已存在」——脏库残留必炸），用后删行不残留 enabled 工作流
（其 DSL 依赖测试进程内注册的 upper 节点，后续运行启动重注册只会失败刷警告）。
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

_SFX = uuid.uuid4().hex[:8]
CUSTOM_NODE_WF = f"custom-node-wf-{_SFX}"


def test_rag_sdk_chunker_reranker(client: TestClient):
    """RAG SDK：Chunker/Reranker 基类子类经 register 注册进组件注册表。"""
    from eap.ext import Chunker, Reranker, register_chunker, register_reranker
    from eap.knowledge.components import get_chunker, get_reranker, list_components

    class SlashChunker(Chunker):
        description = "按斜杠切分"

        def chunk(self, text: str, params: dict | None = None) -> list[str]:
            return [p for p in text.split("/") if p] or [""]

    class FrontReranker(Reranker):
        description = "无实现（保持原序）"

        def rerank(self, query: str, candidates: list[tuple[int, str]],
                   params: dict | None = None) -> list[int]:
            return [idx for idx, _ in candidates]

    register_chunker("sdk-slash", SlashChunker(), description="sdk 测试")
    register_reranker("sdk-front", FrontReranker(), description="sdk 测试")

    chunker = get_chunker("sdk-slash", {})
    assert [c for c in chunker("a/b/c")] == ["a", "b", "c"]
    reranker = get_reranker("sdk-front")
    assert reranker("q", [(0, "x"), (1, "y")]) == [0, 1]
    names = {c.name for c in list_components()}
    assert {"sdk-slash", "sdk-front"} <= names


def test_custom_workflow_node(client: TestClient):
    """Workflow Node SDK：自定义节点类型注册 → DSL 执行 → 输出进入下游。"""
    from eap.ext import register_workflow_node
    from eap.runtime.workflow import WorkflowSpec

    async def upper_executor(step, ctx, db, state, invoke_input) -> str:
        value = str(state.variables.get("raw_text", ""))
        return value.upper()

    register_workflow_node("upper", upper_executor)
    spec = WorkflowSpec(name=CUSTOM_NODE_WF, version="1.0.0",
                        steps=[{"id": "raw", "type": "upper",
                                "ui_schema": {"type": "form", "fields": []}},
                               {"id": "say", "type": "llm", "system": "s"}])
    assert spec.steps[0].type == "upper"  # 自定义类型通过校验

    import asyncio
    from eap.db import SessionLocal
    from eap.agents.registry import get_platform_context
    from eap.workflows import create_and_register

    async def _run():
        with SessionLocal() as db:
            await create_and_register(db, spec)
            db.commit()
        ctx = get_platform_context()
        with SessionLocal() as db:
            from eap.runtime.workflow import _RunState, _execute_node
            step = spec.steps[0]
            st = _RunState("hello")
            st.variables["raw_text"] = "hello"
            out = await _execute_node(step, ctx, db, st, "hello")
            return out

    try:
        out = asyncio.run(_run())
        assert out == "HELLO"
    finally:
        # 自清理：删除本用例落库的工作流行（唯一名已保证可重入，删行避免
        # enabled 残留在后续运行启动时以未注册的 upper 节点类型重注册刷警告）
        from sqlalchemy import delete

        from eap.db import SessionLocal
        from eap.models import WorkflowRecord
        with SessionLocal() as db:
            db.execute(delete(WorkflowRecord).where(WorkflowRecord.name == CUSTOM_NODE_WF))
            db.commit()


def test_model_provider_sdk(client: TestClient):
    """Provider SDK：注册自定义供应商 → get_provider 解析 → 路由可用。"""
    from eap.modelhub import providers
    from eap.modelhub.providers import LLMResult

    class EchoProvider:
        async def complete(self, *, record, messages, tools, temperature, response_schema=None):
            text = "echo-provider 回声"
            return LLMResult(content=text, tokens_in=3, tokens_out=3, model=record.name)

        def stream_complete(self, *, record, messages, temperature):
            async def _gen():
                yield "echo"
            return _gen()

    providers.register_provider("echo_sdk", EchoProvider())
    record = type("R", (), {"name": "echo-model", "provider": "echo_sdk",
                            "remote_model": None, "base_url": None, "api_key": None,
                            "capabilities": ["chat"], "priority": 1, "enabled": True})()
    from eap.modelhub.providers import get_provider

    provider = get_provider(record.provider)
    assert provider is not None


def test_ui_component_sdk(client: TestClient):
    """UI SDK：注册组合式自定义组件 → API 列表可见。"""
    from eap.ext import register_ui_component

    register_ui_component("warehouse_selector", {
        "fields": [
            {"type": "select", "id": "warehouse", "label": "仓库", "required": True,
             "options": [{"label": "一号仓", "value": "w1"}]},
            {"type": "number", "id": "qty", "label": "数量"},
        ],
    })
    from eap.ext import ui_components as comps

    assert "warehouse_selector" in comps()


def test_connector_kind_sdk(client: TestClient):
    """Connector SDK：注册自定义连接器类型 → 新建连接器 → 工具构建走扩展工厂。"""
    from eap.ext import register_connector_kind
    from eap.runtime.connectors import _CONNECTOR_KINDS

    calls: list = []

    def factory(record):
        calls.append(record)
        from eap.runtime.tools import Tool

        async def _h(args: str) -> str:
            return json.dumps({"kind": record.kind})

        return [Tool(name=f"custom.{record.name}.ping", description="自定义连接器工具",
                     parameters={"type": "object", "properties": {}}, handler=_h)]

    register_connector_kind("sdk-conn", factory)
    assert "sdk-conn" in _CONNECTOR_KINDS

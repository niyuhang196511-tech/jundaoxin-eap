"""Extension SDK 契约（docs/unfinished v0.7 / 三十六～四十七节）：各类型扩展的接入契约。

- RAG SDK：Chunker / Reranker 抽象基类（形式化 components.py 的鸭子协议；继承可选）
- Workflow Node SDK：register_workflow_node(kind, executor)——自定义节点类型
- Model Provider SDK：register_provider(name, provider)——新增供应商适配
- Connector SDK：register_connector_kind(kind, factory)——扩展连接器类型
- UI SDK：register_ui_component(id, schema_fragment)——组合式自定义交互组件

全部注册表为进程内单例；扩展经 plugins/bundle 的 register() 调用注册。
"""

from __future__ import annotations

from ..runtime.tools import Tool  # noqa: F401  Tool dataclass 即 Tool SDK 契约（含治理元数据）


# ---------- RAG SDK：Chunker / Reranker 基类 ----------

from ..knowledge.components import register_chunker as _register_chunker
from ..knowledge.components import register_reranker as _register_reranker


def register_chunker(name: str, factory, description: str = "", source: str = "plugin") -> None:
    """注册分块器：factory.chunk(text, params) -> list[str]（或同签名函数）。"""
    _register_chunker(name, factory, description=description, source=source)


def register_reranker(name: str, factory, description: str = "", source: str = "plugin") -> None:
    """注册重排器：factory.rerank(query, candidates, params) -> list[int]（或同签名函数）。"""
    _register_reranker(name, factory, description=description, source=source)


class Chunker:
    """分块器契约：chunk(text, params) -> list[str]。

    继承本类获得类型提示与默认实现；直接提供同签名函数亦可（鸭子类型兼容）。
    """

    description = ""

    def chunk(self, text: str, params: dict | None = None) -> list[str]:
        raise NotImplementedError

    def __call__(self, text: str, params: dict | None = None) -> list[str]:
        return self.chunk(text, params)


class Reranker:
    """重排器契约：rerank(query, candidates, params) -> list[int]（候选下标，相关性降序）。"""

    description = ""

    def rerank(self, query: str, candidates: list[tuple[int, str]],
               params: dict | None = None) -> list[int]:
        raise NotImplementedError

    def __call__(self, query: str, candidates: list[tuple[int, str]],
                 params: dict | None = None) -> list[int]:
        return self.rerank(query, candidates, params)


# ---------- Workflow Node SDK ----------

_WORKFLOW_NODE_EXECUTORS: dict[str, callable] = {}


def register_workflow_node(kind: str, executor) -> None:
    """注册自定义工作流节点类型：executor(step, ctx, db, state, invoke_input) -> str 输出。

    kind 将进入 DSL Step.type 合法集合（内置类型之外）；parallel/loop 体内不允许。
    """
    from ..runtime.workflow import register_custom_node

    register_custom_node(kind, executor)


def workflow_node_executors() -> dict[str, callable]:
    from ..runtime.workflow import custom_node_executors

    return custom_node_executors()


# ---------- Model Provider SDK ----------

def register_provider(name: str, provider) -> None:
    """注册模型供应商适配（complete/stream_complete 协议，见 modelhub.providers.Provider）。"""
    from ..modelhub import providers

    providers.register_provider(name, provider)


# ---------- Connector SDK ----------

def register_connector_kind(kind: str, factory) -> None:
    """注册连接器类型：factory(record) -> list[Tool]（超出 rest/mock-erp 的新连接器形态）。"""
    from ..runtime.connectors import register_connector_kind as _reg

    _reg(kind, factory)


# ---------- UI SDK（MVP）：组合式自定义交互组件 ----------

_UI_COMPONENTS: dict[str, dict] = {}


def register_ui_component(component_id: str, schema_fragment: dict) -> None:
    """注册自定义交互组件：schema_fragment 为组合式 UISchema 片段（fields 列表）。

    interaction schema 引用方式：{"type": "custom", "component": "<id>"}，
    前端以内联子表单渲染组件声明的 fields（无需前端代码）。
    """
    if not isinstance(schema_fragment, dict) or not isinstance(
            schema_fragment.get("fields"), list):
        raise ValueError("ui component 须为含 fields 数组的 UISchema 片段")
    _UI_COMPONENTS[component_id] = schema_fragment


def ui_components() -> dict[str, dict]:
    return dict(_UI_COMPONENTS)

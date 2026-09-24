"""eap-sdk：EAP 平台扩展 SDK 的独立分发包（薄 re-export 层，docs/14 §四）。

设计约束（M50-C 打包就绪态）：
- 零平台代码拷贝：全部契约符号从平台包 eap 运行时再导出，
  `eap.ext.sdk` / `eap.agents.sdk` / `eap.runtime.tools` 仍是单一事实源；
- 需先安装平台包（uv/git 源，见 README）——平台缺失时抛带安装指引的
  ModuleNotFoundError，而非裸 ImportError 栈；
- 版本单一事实源：`__version__` 动态读 `eap.__version__`（构建期包版本
  亦由 setup.py 从平台源码推导，两侧同源，无双处手改）。

契约面（与 docs/14 §二 七类 SDK 对应）：
- RAG SDK：Chunker / Reranker 基类 + register_chunker / register_reranker
- Workflow Node SDK：register_workflow_node / workflow_node_executors
- Model Provider SDK：register_provider
- Connector SDK：register_connector_kind
- UI SDK：register_ui_component / ui_components
- Tool SDK：Tool（eap.runtime.tools 的治理元数据 dataclass）
- Agent SDK：AgentApp / AgentManifest / register_agent / PlatformContext /
  Retriever / retriever_tool / InvokeRequest / InvokeResult
"""

from __future__ import annotations

_INSTALL_HINT = (
    "eap-sdk 是 EAP 平台扩展契约的薄 re-export 层，需要先安装平台包 eap。"
    "安装通道：① 仓库源码 `uv pip install ./eap`（或 `uv add ./eap`）；"
    "② git 源 `uv pip install 'eap-sdk[platform-git]'`（需仓库读取权限）。"
    "详见 sdk/python/README.md 与 docs/14-extension-sdk.md §四。"
)

try:
    # 版本单一事实源：平台包 __version__（构建期版本同源于 setup.py 推导）
    from eap import __version__ as __version__
except ModuleNotFoundError as _exc:
    if _exc.name != "eap":
        raise
    raise ModuleNotFoundError(_INSTALL_HINT) from None

# 以下契约符号全部为再导出（不复制实现）；平台包残缺（子模块缺失）时
# 不做兜底——那属于平台安装损坏，保留原始 traceback 更诚实。
from eap.agents.sdk import (AgentApp, AgentManifest, InvokeRequest, InvokeResult,
                            PlatformContext, Retriever, register_agent, retriever_tool)
from eap.ext import (Chunker, Reranker, register_chunker, register_connector_kind,
                     register_provider, register_reranker, register_ui_component,
                     register_workflow_node, ui_components, workflow_node_executors)
from eap.runtime.tools import Tool

__all__ = [
    "__version__",
    # RAG SDK
    "Chunker", "Reranker", "register_chunker", "register_reranker",
    # Workflow Node SDK
    "register_workflow_node", "workflow_node_executors",
    # Model Provider SDK
    "register_provider",
    # Connector SDK
    "register_connector_kind",
    # UI SDK
    "register_ui_component", "ui_components",
    # Tool SDK
    "Tool",
    # Agent SDK
    "AgentApp", "AgentManifest", "PlatformContext", "Retriever",
    "register_agent", "retriever_tool", "InvokeRequest", "InvokeResult",
]

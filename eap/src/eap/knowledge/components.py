"""RAG 组件注册表（扩展开发体系，docs/04 §1）：手写 Chunker / Embedder / Reranker 的插拔点。

设计：
- 每类组件一个进程内注册表（名称 → 工厂），内置实现启动时注册为默认项
- 插件（plugins/ 目录或 entry_points）可通过 register_* 注入自定义实现
- KBRecord.pipeline JSON 列按 KB 选择组件 + 参数（未配置走内置默认）
- 组件 = 受信代码，进程内执行（与 AgentApp 同一信任边界，不做沙箱）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# ---------- 组件协议（结构化鸭子类型，避免强制继承） ----------

# Chunker:    (text: str, params: dict) -> list[str]
# Embedder:   provider 名称，经 knowledge.embedding.get_embedder 解析
# Reranker:   (query: str, candidates: list[tuple[int, str]], params: dict) -> list[int]


@dataclass
class ComponentInfo:
    name: str
    kind: str  # chunker | reranker
    description: str = ""
    source: str = "builtin"  # builtin | plugin:<dir>


_REGISTRIES: dict[str, dict[str, tuple[Callable, ComponentInfo]]] = {
    "chunker": {},
    "reranker": {},
}


def register_chunker(name: str, factory: Callable[..., list[str]],
                     description: str = "", source: str = "plugin") -> None:
    """注册分块器：factory(text, params) -> list[str]。"""
    _REGISTRIES["chunker"][name] = (factory, ComponentInfo(name, "chunker", description, source))


def register_reranker(name: str, factory: Callable[..., list[int]],
                      description: str = "", source: str = "plugin") -> None:
    """注册重排器：factory(query, candidates, params) -> list[int]（候选索引降序）。"""
    _REGISTRIES["reranker"][name] = (factory, ComponentInfo(name, "reranker", description, source))


def get_chunker(name: str | None, params: dict[str, Any] | None = None) -> Callable[[str], list[str]]:
    """解析分块器为单参可调用（params 已绑定）。name 为空/未知 → 内置默认。"""
    reg = _REGISTRIES["chunker"]
    key = name or "default"
    if key in reg:
        factory, _ = reg[key]
        bound_params = dict(params or {})
        return lambda text: factory(text, bound_params)
    from .chunking import split_text

    return lambda text: split_text(text)


def get_reranker(name: str | None, params: dict[str, Any] | None = None):
    """解析重排器为 (query, candidates) 可调用。name 为空/未知 → 内置 lexical。"""
    reg = _REGISTRIES["reranker"]
    key = name or "lexical"
    if key in reg:
        factory, _ = reg[key]
        bound_params = dict(params or {})
        return lambda query, candidates: factory(query, candidates, bound_params)
    from .rerank import lexical_rerank

    return lexical_rerank


def unregister(kind: str, name: str) -> bool:
    """注销组件（v0.7 插件停用/卸载用）；内置组件不允许注销。"""
    registry = _REGISTRIES.get(kind)
    if registry is None:
        return False
    info = registry.get(name)
    if info is None:
        return False
    if info[1].source == "builtin":
        return False
    registry.pop(name, None)
    return True


def list_components() -> list[ComponentInfo]:
    """全部已注册组件（扩展中心页数据源）。"""
    out: list[ComponentInfo] = []
    for kind in ("chunker", "reranker"):
        for _, (_, info) in sorted(_REGISTRIES[kind].items()):
            out.append(info)
    return out


# ---------- 内置实现注册（默认项） ----------

def _register_builtins() -> None:
    from .chunking import split_text
    from .rerank import lexical_rerank

    register_chunker("default", lambda text, params: split_text(
        text, target=int(params.get("target", 400)), overlap=int(params.get("overlap", 60))),
        description="段落聚合 + 长段硬切（内置，docs/04 §1.2）", source="builtin")
    register_chunker("fixed", lambda text, params: [
        text[i:i + int(params.get("size", 300))]
        for i in range(0, len(text), int(params.get("size", 300)) - int(params.get("overlap", 0)))
        if text[i:i + int(params.get("size", 300))].strip()],
        description="定长滑窗切块", source="builtin")
    register_reranker("lexical", lambda query, candidates, params: lexical_rerank(query, candidates),
                      description="词面覆盖度重排（内置，确定性）", source="builtin")


_register_builtins()

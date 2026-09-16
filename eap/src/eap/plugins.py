"""插件目录加载器（扩展开发体系）：plugins/ 目录下的 Python 包即插件。

约定：EAP_PLUGINS_DIR（默认 ./plugins）下每个子目录是一个插件包，包内 manifest.py
可选暴露：

    EAP_PLUGIN = {
        "name": "my-plugin",              # 必填
        "kind": "tool",                   # tool | rag | agent（agent 走 register_agent）
        "description": "…",
        "register": callable,             # 必填：无参函数，import 后调用完成注册
                                          #   tool → register_workflow_tool(name, factory)
                                          #   rag  → register_chunker / register_reranker
    }

信任边界：插件 = 受信代码，进程内 import 执行（与内置 AgentApp 同一边界，不做沙箱）。
与 entry_points 发现通道（agents SDK）并存：前者面向开发期目录热载，后者面向发行包。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field

from .config import get_settings


@dataclass
class PluginInfo:
    name: str
    kind: str
    description: str = ""
    module: str = ""
    source: str = "plugin"
    status: str = "loaded"  # loaded | failed
    error: str = ""
    exposes: list = field(default_factory=list)  # 注册的组件/工具名


_LOADED: list[PluginInfo] = []


def plugins_dir() -> str:
    return get_settings().plugins_dir


def loaded_plugins() -> list[PluginInfo]:
    return list(_LOADED)


def load_plugins() -> list[PluginInfo]:
    """扫描插件目录并逐个加载；单个插件失败不影响其他插件与主流程。"""
    _LOADED.clear()
    d = plugins_dir()
    if not os.path.isdir(d):
        return _LOADED
    from .knowledge import components as rag_components
    from .runtime.workflow import register_workflow_tool

    for entry in sorted(os.listdir(d)):
        path = os.path.join(d, entry)
        if not os.path.isdir(path) or entry.startswith(("_", ".")):
            continue
        info = PluginInfo(name=entry, kind="unknown", module=entry)
        _LOADED.append(info)
        try:
            if os.path.isfile(os.path.join(path, "__init__.py")):
                module = importlib.import_module(entry)
            else:  # 单文件/非包目录：按路径加载 manifest.py（约定入口）
                manifest_path = os.path.join(path, "manifest.py")
                if not os.path.isfile(manifest_path):
                    raise ValueError("缺少 __init__.py 或 manifest.py")
                spec = importlib.util.spec_from_file_location(f"eap_plugin_{entry}", manifest_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            info.kind = "package"
            manifest = getattr(module, "EAP_PLUGIN", None)
            if not isinstance(manifest, dict) or not manifest.get("name"):
                info.status = "loaded"  # 普通包无 manifest：仅加载，不注册
                continue
            info.name = str(manifest["name"])
            info.kind = str(manifest.get("kind", "unknown"))
            info.description = str(manifest.get("description", ""))
            register = manifest.get("register")
            if not callable(register):
                raise ValueError("manifest.register 不可调用")
            before_tools = set()
            before_comps = {c.name for c in rag_components.list_components()}
            register()
            if info.kind == "tool":
                # 工具注册表不回读清单：由插件在 description 里自述
                info.exposes = [info.name]
            elif info.kind == "rag":
                after = {c.name for c in rag_components.list_components()}
                info.exposes = sorted(after - before_comps)
            _ = before_tools
            info.status = "loaded"
        except Exception as e:  # noqa: BLE001 单插件失败不拖垮启动
            info.status = "failed"
            info.error = str(e)[:500]
    return _LOADED


def reload_plugins() -> list[PluginInfo]:
    """热重载：清模块缓存后重新扫描（开发期改代码即生效）。"""
    for info in list(_LOADED):
        for mod in [m for m in sys.modules if m == info.module or m.startswith(f"eap_plugin_{info.module}")]:
            sys.modules.pop(mod, None)
    return load_plugins()

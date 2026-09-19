"""插件目录加载器（扩展开发体系）：plugins/ 目录下的 Python 包即插件。

约定：EAP_PLUGINS_DIR（默认 ./plugins）下每个子目录是一个插件包，包内 manifest.py
可选暴露（两种形式，v0.7 起推荐统一 Manifest）：

    # 统一 Manifest（v0.7）：ExtensionManifest 字段 + register
    EXTENSION_MANIFEST = {"type": "tool", "name": "my-tool", "version": "1.0.0", ...}

    # 旧 EAP_PLUGIN dict（向后兼容，自动归一化）
    EAP_PLUGIN = {"name": "my-plugin", "kind": "tool", "description": "…",
                  "register": callable}   # 必填：无参函数，import 后调用完成注册
                                          #   tool → register_workflow_tool(name, factory)
                                          #   rag  → register_chunker / register_reranker

信任边界：插件 = 受信代码，进程内 import 执行（与内置 AgentApp 同一边界，不做沙箱）。
与 entry_points 发现通道（agents SDK）并存：前者面向开发期目录热载，后者面向发行包。

加载结果与 ExtensionRecord（持久注册表）同步：记录 state/exposes/error，
enable/disable 状态跨重启保持（disabled 的插件不执行 register）。
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
    status: str = "loaded"  # loaded | failed | skipped_disabled
    error: str = ""
    exposes: list = field(default_factory=list)  # 注册的组件/工具名
    type: str = "package"  # 统一 Manifest type（v0.7）
    version: str = "0.0.0"
    manifest: dict = field(default_factory=dict)  # 统一 Manifest dump


_LOADED: list[PluginInfo] = []


def plugins_dir() -> str:
    return get_settings().plugins_dir


def loaded_plugins() -> list[PluginInfo]:
    return list(_LOADED)


def _record_states() -> dict[str, tuple[str, str]]:
    """读 ExtensionRecord 的 (state, error)（name → 状态），加载器据此保持启停。"""
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import ExtensionRecord

    try:
        with SessionLocal() as db:
            rows = db.scalars(select(ExtensionRecord)).all()
            return {r.name: (r.state, r.error or "") for r in rows}
    except Exception:
        return {}  # 表未建（无库环境）等：默认全部按 registered 处理


def _sync_records(infos: list[PluginInfo]) -> None:
    """加载结果与 ExtensionRecord 同步（upsert；保留既有 enabled/disabled 状态）。"""
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import ExtensionRecord

    try:
        with SessionLocal() as db:
            existing = {r.name: r for r in db.scalars(select(ExtensionRecord)).all()}
            for info in infos:
                record = existing.get(info.name)
                if record is None:
                    record = ExtensionRecord(name=info.name)
                    db.add(record)
                record.type = info.type
                record.version = info.version
                record.title = info.manifest.get("title", "") if info.manifest else info.description
                record.description = info.description
                record.manifest = info.manifest or {}
                record.source = "plugin_dir" if info.source in ("plugin", "package") else info.source
                record.module = info.module
                record.exposes = info.exposes
                if info.status == "failed":
                    record.state = "failed"
                    record.error = info.error
                elif record.state == "disabled":
                    record.state = "disabled"  # 运营者禁用状态跨重启保持
                else:
                    # 新登记 / registered / enabled / 从 failed 恢复 → enabled
                    record.state = "enabled"
                record.error = info.error if info.status == "failed" else ""
            db.commit()
    except Exception:
        pass  # 注册表同步失败不影响插件加载主流程


def load_plugins() -> list[PluginInfo]:
    """扫描插件目录并逐个加载；单个插件失败不影响其他插件与主流程。"""
    _LOADED.clear()
    d = plugins_dir()
    if not os.path.isdir(d):
        _sync_records(_LOADED)
        return _LOADED
    from .knowledge import components as rag_components

    states = _record_states()
    for entry in sorted(os.listdir(d)):
        path = os.path.join(d, entry)
        if not os.path.isdir(path) or entry.startswith(("_", ".")):
            continue
        info = PluginInfo(name=entry, kind="unknown", module=entry)
        _LOADED.append(info)
        saved_state = states.get(entry, ("registered", ""))[0]
        if saved_state == "disabled":
            # 运营者禁用：加载元信息但不执行注册
            info.status = "skipped_disabled"
            manifest = _read_manifest_meta(path)
            if manifest is not None:
                info.name = manifest.name
                info.kind = manifest.type
                info.type = manifest.type
                info.version = manifest.version
                info.description = manifest.description
                info.manifest = manifest.model_dump()
            continue
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

            # 统一 Manifest（v0.7）优先；旧 EAP_PLUGIN dict 归一化兼容
            from .runtime.extension_manifest import (ExtensionManifest,
                                                     check_runtime_compatibility,
                                                     normalize_eap_plugin)

            unified_raw = getattr(module, "EXTENSION_MANIFEST", None)
            eap_plugin = getattr(module, "EAP_PLUGIN", None)
            manifest: ExtensionManifest | None = None
            if isinstance(unified_raw, dict):
                manifest = ExtensionManifest(**unified_raw)
            elif isinstance(eap_plugin, dict) and eap_plugin.get("name"):
                manifest = normalize_eap_plugin(eap_plugin)

            if manifest is not None:
                check_runtime_compatibility(manifest.min_version())
                info.name = manifest.name
                info.type = manifest.type
                info.kind = manifest.type
                info.version = manifest.version
                info.description = manifest.description
                info.manifest = manifest.model_dump()

            register = getattr(module, "register", None)
            if register is None and isinstance(eap_plugin, dict):
                register = eap_plugin.get("register")
            if not callable(register):
                if manifest is None:
                    info.status = "loaded"  # 普通包无 manifest：仅加载，不注册
                    continue
                raise ValueError("manifest.register 不可调用")

            before_comps = {c.name for c in rag_components.list_components()}
            register()
            if info.type == "tool" or (manifest is None and info.kind == "tool"):
                info.exposes = [info.name]
            elif info.type == "rag" or (manifest is None and info.kind == "rag"):
                after = {c.name for c in rag_components.list_components()}
                info.exposes = sorted(after - before_comps)
            info.status = "loaded"
        except Exception as e:  # noqa: BLE001 单插件失败不拖垮启动
            info.status = "failed"
            info.error = str(e)[:500]
    _sync_records(_LOADED)
    return _LOADED


def _read_manifest_meta(path: str):
    """禁用态读取 manifest 元信息（不执行 register）。"""
    from .runtime.extension_manifest import ExtensionManifest, normalize_eap_plugin

    for candidate in ("manifest.py", "__init__.py"):
        manifest_path = os.path.join(path, candidate)
        if not os.path.isfile(manifest_path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"eap_meta_{os.path.basename(path)}", manifest_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            unified_raw = getattr(module, "EXTENSION_MANIFEST", None)
            if isinstance(unified_raw, dict):
                return ExtensionManifest(**unified_raw)
            eap_plugin = getattr(module, "EAP_PLUGIN", None)
            if isinstance(eap_plugin, dict) and eap_plugin.get("name"):
                return normalize_eap_plugin(eap_plugin)
        except Exception:
            return None
    return None


def reload_plugins() -> list[PluginInfo]:
    """热重载：清空各注册表副作用 + 模块缓存后重新扫描（停用的插件不重建注册项）。"""
    from .knowledge import components as rag_components
    from .runtime.workflow import _WORKFLOW_TOOLS

    plugin_names = {info.name for info in _LOADED}
    # 清理插件注册的副作用（工具 / RAG 组件），启用态插件随后由 load 重建
    for name in [n for n in _WORKFLOW_TOOLS if n in plugin_names]:
        _WORKFLOW_TOOLS.pop(name, None)
    for comp in list(rag_components.list_components()):
        if comp.source.startswith("plugin"):
            rag_components.unregister(comp.kind, comp.name)
    for info in list(_LOADED):
        for mod in [m for m in sys.modules if m == info.module or m.startswith(f"eap_plugin_{info.module}")]:
            sys.modules.pop(mod, None)
    return load_plugins()

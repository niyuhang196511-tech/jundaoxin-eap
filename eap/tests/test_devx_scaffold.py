"""开发者体验四件套（M48-B）：scaffold 新增四类模板 + pack 打包 + 示例库可运行性。

- workflow-node / model-provider / connector / ui-component 四类脚手架：
  生成物结构（manifest.json + manifest.py + README.md）、统一 Manifest 校验、
  关键注册调用、经插件加载器真实注册进平台注册表、pack → read_bundle 打包回读；
- examples/rag-pipeline：RAG SDK reranker 示例注册 + 确定性重排输出；
- examples/task-flow：任务引擎端到端（提交 → worker 执行 → COMPLETED）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_EAP_ROOT = Path(__file__).resolve().parents[1]

NEW_KINDS = {
    "workflow-node": "register_workflow_node(",
    "model-provider": "register_provider(",
    "connector": "register_connector_kind(",
    "ui-component": "register_ui_component(",
}

_KIND_EXT_TYPE = {
    "workflow-node": "workflow_node",
    "model-provider": "model_provider",
    "connector": "connector",
    "ui-component": "ui",
}


def _load_module_file(path: Path, module_name: str):
    """按平台插件加载器的方式从文件路径加载模块（目录名含连字符不可作为包导入）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_registered(kind: str, name: str) -> None:
    if kind == "workflow-node":
        from eap.runtime.workflow import custom_node_executors

        assert name in custom_node_executors()
    elif kind == "model-provider":
        from eap.modelhub import providers

        assert name in providers._CUSTOM_PROVIDERS
    elif kind == "connector":
        from eap.runtime.connectors import _CONNECTOR_KINDS

        assert name in _CONNECTOR_KINDS
    elif kind == "ui-component":
        from eap.ext import ui_components

        assert name in ui_components()


def _cleanup_registry(kind: str, name: str) -> None:
    if kind == "workflow-node":
        from eap.runtime.workflow import _CUSTOM_NODE_EXECUTORS

        _CUSTOM_NODE_EXECUTORS.pop(name, None)
    elif kind == "model-provider":
        from eap.modelhub import providers

        providers._CUSTOM_PROVIDERS.pop(name, None)
    elif kind == "connector":
        from eap.runtime.connectors import _CONNECTOR_KINDS

        _CONNECTOR_KINDS.pop(name, None)
    elif kind == "ui-component":
        from eap.ext.sdk import _UI_COMPONENTS

        _UI_COMPONENTS.pop(name, None)


# ---------- 脚手架生成物结构 ----------


@pytest.mark.parametrize("kind", sorted(NEW_KINDS))
def test_scaffold_new_kind_templates(tmp_path, kind):
    """四类新模板：生成文件齐全、manifest 合法（格式即校验）、含关键注册调用。"""
    from eap.runtime.extension_manifest import EXTENSION_TYPES, ExtensionManifest
    from eap.scaffold import scaffold

    name = f"demo-{kind}"
    written = scaffold(kind, name, str(tmp_path))
    assert {Path(p).name for p in written} == {"manifest.json", "manifest.py", "README.md"}

    raw = json.loads((tmp_path / name / "manifest.json").read_text(encoding="utf-8"))
    assert raw["type"] in EXTENSION_TYPES
    assert raw["name"] == name
    ExtensionManifest(**raw)  # 统一 Manifest 格式即校验

    entry = (tmp_path / name / "manifest.py").read_text(encoding="utf-8")
    assert NEW_KINDS[kind] in entry
    assert "EAP_PLUGIN" in entry
    readme = (tmp_path / name / "README.md").read_text(encoding="utf-8")
    assert name in readme  # README 按生成名定制


# ---------- pack 打包（.eapext）对新增类型可用 ----------


@pytest.mark.parametrize("kind", sorted(NEW_KINDS))
def test_scaffold_new_kind_pack_bundle(tmp_path, kind):
    """scaffold pack：目录 → .eapext → read_bundle 校验通过（manifest + 入口布局）。"""
    from eap.runtime.bundles import pack_bundle, read_bundle
    from eap.scaffold import scaffold

    name = f"pack-{kind}"
    scaffold(kind, name, str(tmp_path))
    out = tmp_path / "out.eapext"
    assert pack_bundle(str(tmp_path / name), str(out)) == str(out)

    manifest, entries = read_bundle(out.read_bytes())
    assert manifest["name"] == name
    assert {"manifest.json", "manifest.py", "README.md"} <= {n for n, _ in entries}


# ---------- 生成物经插件加载器真实注册 ----------


@pytest.mark.parametrize("kind", sorted(NEW_KINDS))
def test_scaffold_new_kind_plugin_load_registers(tmp_path, kind):
    """插件目录加载 → register 副作用进入平台注册表（节点/供应商/连接器/UI 组件）。"""
    from eap import plugins as plugins_mod
    from eap.config import get_settings
    from eap.scaffold import scaffold

    name = f"plug-{kind}"
    scaffold(kind, name, str(tmp_path))
    old = get_settings().plugins_dir
    get_settings().__dict__["plugins_dir"] = str(tmp_path)
    try:
        infos = plugins_mod.load_plugins()
        mine = next((p for p in infos if p.name == name), None)
        assert mine is not None and mine.status == "loaded", mine
        assert mine.type == _KIND_EXT_TYPE[kind], mine
        _assert_registered(kind, name)
    finally:
        get_settings().__dict__["plugins_dir"] = old
        _cleanup_registry(kind, name)


# ---------- 模板骨架可直接执行（契约签名正确） ----------


def test_scaffold_workflow_node_executor_runs(tmp_path):
    from eap.scaffold import scaffold

    scaffold("workflow-node", "exec-node", str(tmp_path))
    module = _load_module_file(tmp_path / "exec-node" / "manifest.py", "devx_exec_node")

    async def run() -> str:
        state = SimpleNamespace(variables={})
        return await module.executor(SimpleNamespace(id="n1"), None, None, state, "hello")

    out = asyncio.run(run())
    assert "HELLO" in out and "n1" in out  # executor(step, ctx, db, state, invoke_input) -> str


def test_scaffold_model_provider_complete_runs(tmp_path):
    from eap.scaffold import scaffold

    scaffold("model-provider", "exec-prov", str(tmp_path))
    module = _load_module_file(tmp_path / "exec-prov" / "manifest.py", "devx_exec_prov")

    record = SimpleNamespace(name="rec", base_url="http://mock", api_key=None,
                             remote_model="m")

    async def run():
        result = await module.MyProvider().complete(
            record=record, messages=[{"role": "user", "content": "hi there"}],
            tools=None, temperature=0.1)
        stream = module.MyProvider().stream_complete(
            record=record, messages=[{"role": "user", "content": "a b"}], temperature=0.1)
        return result, "".join([c async for c in stream])

    result, streamed = asyncio.run(run())
    assert "hi there" in (result.content or "") and result.model == "rec"
    assert streamed.strip()  # stream_complete 为 async 生成器且逐段产出


def test_scaffold_connector_factory_builds_tools(tmp_path):
    from eap.scaffold import scaffold

    scaffold("connector", "exec-conn", str(tmp_path))
    module = _load_module_file(tmp_path / "exec-conn" / "manifest.py", "devx_exec_conn")

    record = SimpleNamespace(
        name="erp", base_url="http://erp.local", header_name="X-Key", api_key=None,
        endpoints=[{"tool_name": "erp.ping", "name": "ping", "method": "GET", "path": "/ping",
                    "description": "ping 端点", "params": [{"name": "q", "type": "string"}]}])
    tools = module.factory(record)
    assert [t.name for t in tools] == ["erp.ping"]  # 与内置 rest 连接器同名约定
    out = json.loads(tools[0].handler('{"q": "v"}'))
    assert out["endpoint"] == "ping" and out["args"] == {"q": "v"}
    assert out["auth_header"] == "X-Key"  # 鉴权占位取 record.header_name


def test_scaffold_ui_component_registers(tmp_path):
    from eap.ext import ui_components
    from eap.ext.sdk import _UI_COMPONENTS
    from eap.scaffold import scaffold

    scaffold("ui-component", "exec-ui", str(tmp_path))
    module = _load_module_file(tmp_path / "exec-ui" / "manifest.py", "devx_exec_ui")

    module.EAP_PLUGIN["register"]()
    try:
        frag = ui_components()["exec-ui"]
        assert isinstance(frag.get("fields"), list) and frag["fields"]
    finally:
        _UI_COMPONENTS.pop("exec-ui", None)


# ---------- 示例库（M48-B 新增两个可运行示例） ----------


def test_example_rag_pipeline_registers_and_reranks():
    """examples/rag-pipeline：经插件入口注册 reranker → 确定性重排。"""
    from eap.knowledge.components import get_reranker, unregister

    module = _load_module_file(_EAP_ROOT / "examples" / "rag-pipeline" / "manifest.py",
                               "devx_example_rag_pipeline")
    module.EAP_PLUGIN["register"]()
    try:
        rerank = get_reranker("keyword-rerank")
        assert rerank("库存 查询", [(0, "天气很好"), (1, "库存查询接口"), (2, "周报模板")]) == [1, 0, 2]
        assert rerank("库存 查询", [(0, "库存清点与查询"), (1, "天气很好")]) == [0, 1]
    finally:
        unregister("reranker", "keyword-rerank")


def test_example_task_flow_runs_end_to_end():
    """examples/task-flow：任务引擎端到端——提交 → worker 执行 → COMPLETED + 结果快照。"""
    module = _load_module_file(_EAP_ROOT / "examples" / "task-flow" / "task_flow_demo.py",
                               "devx_example_task_flow")
    snapshot = asyncio.run(module.run_demo(steps=3))
    assert snapshot["state"] == "COMPLETED", snapshot
    assert snapshot["result"]["done"] == ["step-1", "step-2", "step-3"]
    assert snapshot["result"]["echo"] == "hello task-flow"

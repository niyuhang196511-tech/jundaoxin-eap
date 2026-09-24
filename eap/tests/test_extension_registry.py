"""统一扩展 Manifest + 注册表测试（v0.7-M27）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def test_extension_manifest_validation():
    """统一 Manifest 校验：非法 type / min_version / 正常路径。"""
    from eap.runtime.extension_manifest import ExtensionManifest, check_runtime_compatibility

    m = ExtensionManifest(type="tool", name="my-tool", version="1.0.0",
                          runtime={"min_version": "0.6.0"})
    assert m.min_version() == "0.6.0"
    with pytest.raises(Exception, match="未知扩展类型"):
        ExtensionManifest(type="nope", name="x-tool", version="1.0.0")
    with pytest.raises(Exception, match="min_version"):
        ExtensionManifest(type="tool", name="x-tool", version="1.0.0",
                          runtime={"min_version": "1.0"})
    # 当前 0.6.0 满足 ≥0.6.0 但不满足 ≥99.0.0
    check_runtime_compatibility("0.6.0")
    with pytest.raises(ValueError, match="升级 EAP"):
        check_runtime_compatibility("99.0.0")


def test_eap_plugin_normalization():
    """旧 EAP_PLUGIN dict → 统一 Manifest 归一化兼容。"""
    from eap.runtime.extension_manifest import normalize_eap_plugin

    m = normalize_eap_plugin({"name": "legacy-tool", "kind": "tool",
                              "description": "旧格式插件"})
    assert m.type == "tool" and m.version == "0.0.0" and m.description == "旧格式插件"
    # 未知 kind 归一为 package
    m2 = normalize_eap_plugin({"name": "weird", "kind": "no-such-kind"})
    assert m2.type == "package"


def test_plugin_load_syncs_extension_record(client: TestClient, tmp_path, monkeypatch):
    """插件目录加载后与 ExtensionRecord 同步（含 exposes 与统一 manifest）。"""

    from eap.config import get_settings

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "greeting-ext").mkdir()
    (plugin_dir / "greeting-ext" / "manifest.py").write_text(
        'EXTENSION_MANIFEST = {"type": "tool", "name": "greeting-ext", "version": "1.2.3",\n'
        '                    "title": "问候工具"}\n'
        'from eap.runtime.tools import Tool\n'
        'from eap.runtime.workflow import register_workflow_tool\n'
        'async def _h(args):\n'
        '    return "{}"\n'
        'def register():\n'
        '    register_workflow_tool("greeting-ext", lambda: Tool(\n'
        '        name="greeting-ext", description="问候",\n'
        '        parameters={"type": "object", "properties": {}}, handler=_h))\n',
        encoding="utf-8")
    monkeypatch.setattr(get_settings(), "plugins_dir", str(plugin_dir), raising=False)

    from eap.plugins import load_plugins

    load_plugins()

    rows = client.get("/api/v1/extensions/registry", headers=AUTH).json()
    row = next(r for r in rows if r["name"] == "greeting-ext")
    assert row["type"] == "tool" and row["version"] == "1.2.3"
    assert row["state"] == "enabled"
    assert "greeting-ext" in row["exposes"]

    # manifest 端点
    mf = client.get("/api/v1/extensions/registry/greeting-ext/manifest", headers=AUTH).json()
    assert mf["manifest"]["type"] == "tool"
    assert mf["manifest"]["title"] == "问候工具"


def test_extension_enable_disable_state_machine(client: TestClient, tmp_path, monkeypatch):
    """enable/disable 状态机：disable 后热重载不执行 register；enable 恢复。"""
    from eap.config import get_settings
    from eap.plugins import load_plugins, reload_plugins
    from eap.runtime.workflow import _WORKFLOW_TOOLS

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "toggle-ext").mkdir()
    (plugin_dir / "toggle-ext" / "manifest.py").write_text(
        'EXTENSION_MANIFEST = {"type": "tool", "name": "toggle-ext", "version": "1.0.0"}\n'
        'from eap.runtime.tools import Tool\n'
        'from eap.runtime.workflow import register_workflow_tool\n'
        'async def _h(args):\n'
        '    return "{}"\n'
        'def register():\n'
        '    register_workflow_tool("toggle-ext", lambda: Tool(\n'
        '        name="toggle-ext", description="t",\n'
        '        parameters={"type": "object", "properties": {}}, handler=_h))\n',
        encoding="utf-8")
    monkeypatch.setattr(get_settings(), "plugins_dir", str(plugin_dir), raising=False)
    load_plugins()
    assert "toggle-ext" in _WORKFLOW_TOOLS

    # 停用 → 热重载后工具从注册表消失
    assert client.post("/api/v1/extensions/registry/toggle-ext/disable",
                       headers=AUTH).status_code == 200
    reload_plugins()
    assert "toggle-ext" not in _WORKFLOW_TOOLS
    row = next(r for r in client.get("/api/v1/extensions/registry", headers=AUTH).json()
               if r["name"] == "toggle-ext")
    assert row["state"] == "disabled"

    # 启用 → 恢复
    assert client.post("/api/v1/extensions/registry/toggle-ext/enable",
                       headers=AUTH).status_code == 200
    reload_plugins()
    assert "toggle-ext" in _WORKFLOW_TOOLS

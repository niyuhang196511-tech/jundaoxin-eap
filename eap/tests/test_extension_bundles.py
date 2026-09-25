"""Extension Bundle 测试（v0.7-M28）：安装/升级/卸载生命周期 + 安全校验。

M52-D 可重入（API 端点用例）：bundle 名加模块级 uuid 后缀 + 测后经卸载端点
删除 ExtensionRecord——manifest.version 是严格 semver（无后缀空间），同名同版本
重装被「拒绝降级安装」挡住（脏库残留必 409）；卸载兜底防安装记录累积。
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

_SFX = uuid.uuid4().hex[:8]
API_BUNDLE = f"api-bundle-tool-{_SFX}"

TOOL_CODE = (
    "from eap.runtime.tools import Tool\n"
    "from eap.runtime.workflow import register_workflow_tool\n"
    "async def _h(args):\n"
    "    return '{}'\n"
    "def register():\n"
    "    register_workflow_tool('bundle-tool', lambda: Tool(\n"
    "        name='bundle-tool', description='bundle 工具',\n"
    "        parameters={'type': 'object', 'properties': {}}, handler=_h))\n"
)


def _bundle(name: str, version: str, code: str = TOOL_CODE,
            manifest_extra: dict | None = None) -> bytes:
    manifest = {"type": "tool", "name": name, "version": version,
                "title": name, "runtime": {"min_version": "0.6.0"},
                "permissions": [], **(manifest_extra or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("manifest.py", f"EXTENSION_MANIFEST = {json.dumps(manifest)}\n" + code)
    return buf.getvalue()


def test_bundle_install_upgrade_uninstall_roundtrip(tmp_path, monkeypatch):
    """安装 → 工具注册 → 升级 → 卸载 全生命周期（不走 HTTP，直接用运行时）。"""
    from eap.db import init_db
    from eap.runtime.bundles import install_bundle, uninstall_bundle
    from eap.runtime.workflow import _WORKFLOW_TOOLS

    init_db()  # 建 extension_records 表（本测试不走 TestClient lifespan）
    plugins_root = str(tmp_path / "plugins")
    monkeypatch.setenv("EAP_PLUGINS_DIR", plugins_root)
    from eap.config import get_settings as _gs

    _gs.cache_clear()

    data = _bundle("bundle-tool", "1.0.0")
    result = install_bundle(data, plugins_root=plugins_root)
    assert result["state"] in ("enabled", "registered")
    assert "bundle-tool" in _WORKFLOW_TOOLS

    # 同版本重复安装 → 409 语义（ValueError 不高于已装版本）
    with pytest.raises(ValueError, match="拒绝降级安装"):
        install_bundle(_bundle("bundle-tool", "1.0.0"), plugins_root=plugins_root)

    # 升级 1.0.0 → 1.1.0
    result = install_bundle(_bundle("bundle-tool", "1.1.0"), plugins_root=plugins_root)
    assert result["version"] == "1.1.0"

    # 卸载
    result = uninstall_bundle("bundle-tool", plugins_root=plugins_root)
    assert result["uninstalled"] is True
    assert "bundle-tool" not in _WORKFLOW_TOOLS
    assert not (Path(plugins_root) / "bundle-tool").exists()


def test_bundle_security_rejections(tmp_path):
    """安装安全：路径穿越 / 缺 manifest / 非法 min_version 全拒绝。"""
    from eap.runtime.bundles import install_bundle, read_bundle

    root = str(tmp_path / "plugins")

    # 路径穿越
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../../evil.py", "x = 1")
        zf.writestr("manifest.json", json.dumps(
            {"type": "tool", "name": "evil", "version": "1.0.0"}))
    with pytest.raises(ValueError, match="非法路径"):
        read_bundle(evil.getvalue())
    with pytest.raises(ValueError):
        install_bundle(evil.getvalue(), plugins_root=root)

    # 缺 manifest
    nomani = io.BytesIO()
    with zipfile.ZipFile(nomani, "w") as zf:
        zf.writestr("manifest.py", "register = lambda: None")
    with pytest.raises(ValueError, match="manifest"):
        read_bundle(nomani.getvalue())

    # 非法 min_version
    bad_ver = io.BytesIO()
    with zipfile.ZipFile(bad_ver, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"type": "tool", "name": "v-bad", "version": "1.0.0",
             "runtime": {"min_version": "not-semver"}}))
        zf.writestr("manifest.py", "register = lambda: None")
    with pytest.raises(ValueError, match="min_version"):
        read_bundle(bad_ver.getvalue())


def test_bundle_install_api_endpoint(client: TestClient, tmp_path, monkeypatch):
    """安装端点（multipart）：安装后注册表可见 + 审计落库。"""
    from eap.config import get_settings

    monkeypatch.setenv("EAP_PLUGINS_DIR", str(tmp_path / "plugins"))
    get_settings.cache_clear()
    try:
        data = _bundle(API_BUNDLE, "1.0.0")
        resp = client.post("/api/v1/extensions/install", headers=AUTH,
                           files={"file": (f"{API_BUNDLE}.eapext", data,
                                           "application/octet-stream")})
        assert resp.status_code == 200, resp.text
        rows = client.get("/api/v1/extensions/registry", headers=AUTH).json()
        assert any(r["name"] == API_BUNDLE and r["source"] == "plugin_dir" for r in rows)
        logs = client.get("/api/v1/audit", headers=AUTH,
                          params={"action": "extension.install"}).json()
        assert logs
    finally:
        # 自清理：卸载删除 ExtensionRecord（安装记录残留会挡后续同名安装；
        # 须在 settings 仍指向本用例 plugins 目录时执行）
        client.delete(f"/api/v1/extensions/registry/{API_BUNDLE}", headers=AUTH)
        get_settings.cache_clear()

"""扩展开发体系测试：插件目录加载 / 工具清单与试运行 / RAG 组件注册表 / 脚手架 / stdio MCP。"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_extensions_tools_and_invoke(client: TestClient):
    """工具清单含注册的工作流工具；试运行端点可执行。"""
    from eap.runtime.tools import Tool
    from eap.runtime.workflow import register_workflow_tool

    async def handler(args: str) -> str:
        return '{"ok": true}'

    register_workflow_tool("ext.test-tool", lambda: Tool(
        name="ext.test-tool", description="扩展测试工具",
        parameters={"type": "object", "properties": {}}, handler=handler))

    tools = client.get("/api/v1/extensions/tools", headers=AUTH).json()
    mine = next((t for t in tools if t["name"] == "ext.test-tool"), None)
    assert mine is not None and mine["parameters"].get("type") == "object", tools

    resp = client.post("/api/v1/extensions/tools/ext.test-tool/invoke", headers=AUTH,
                       json={"args": {}})
    assert resp.status_code == 200
    assert resp.json()["output"] == '{"ok": true}'

    # 不存在的工具 → 404
    resp = client.post("/api/v1/extensions/tools/nope.missing/invoke", headers=AUTH,
                       json={"args": {}})
    assert resp.status_code == 404


def test_rag_components_registry(client: TestClient):
    """内置 chunker/reranker 已注册；自定义组件注册后出现在清单并参与检索。"""
    from eap.knowledge.components import get_chunker, get_reranker, list_components, register_chunker

    infos = list_components()
    kinds = {c.kind for c in infos}
    assert {"chunker", "reranker"} <= kinds
    assert any(c.name == "default" and c.source == "builtin" for c in infos)

    register_chunker("ext.markdown", lambda text, params: [
        line for line in text.splitlines() if line.startswith("#")],
        description="按标题行切块（扩展测试）", source="plugin")

    chunk = get_chunker("ext.markdown")
    pieces = chunk("# 标题一\n正文\n\n# 标题二\n正文2")
    assert pieces == ["# 标题一", "# 标题二"]
    assert get_chunker(None) is not None  # 默认兜底
    assert get_reranker("nonexistent") is not None  # 未知 → lexical 兜底

    comps = client.get("/api/v1/extensions/rag-components", headers=AUTH).json()
    assert any(c["name"] == "ext.markdown" for c in comps)


def test_kb_pipeline_uses_custom_chunker(client: TestClient):
    """KB.pipeline 选型自定义 chunker：摄入后分块结果按组件逻辑。"""
    # 建库（带 pipeline）
    resp = client.post("/api/v1/kb", headers=AUTH, json={
        "name": "ext-kb", "title": "扩展分块测试",
        "pipeline": {"chunker": {"name": "ext.markdown"}},
    })
    assert resp.status_code == 200, resp.text

    # 摄入 markdown：按标题行分块（2 块）→ 检索验证分块内容
    resp = client.post("/api/v1/kb/ext-kb/documents", headers=AUTH,
                       json={"title": "md", "text": "# 标题一\n正文一\n# 标题二\n正文二"})
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/v1/kb/ext-kb/retrieve", headers=AUTH,
                       json={"query": "标题二", "top_k": 5})
    assert resp.status_code == 200, resp.text
    hits = resp.json().get("hits", [])
    assert any("# 标题二" in h["content"] for h in hits), hits


def test_plugin_dir_load_and_hot_reload(client: TestClient, tmp_path):
    """plugins/ 目录 manifest 插件：加载 → 工具可用 → reload 生效。"""

    plugin_dir = tmp_path / "plugins" / "echo-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.py").write_text(
        '''
from eap.runtime.tools import Tool
from eap.runtime.workflow import register_workflow_tool


async def handler(args_json: str) -> str:
    import json

    args = json.loads(args_json) if args_json.strip() else {}
    return json.dumps({"echo": args.get("query", "")}, ensure_ascii=False)


EAP_PLUGIN = {
    "name": "echo-plugin",
    "kind": "tool",
    "description": "回声工具（测试插件）",
    "register": lambda: register_workflow_tool(
        "echo-plugin.echo",
        lambda: Tool(name="echo-plugin.echo", description="回声",
                     parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                     handler=handler)),
}
''',
        encoding="utf-8")

    from eap import plugins as plugins_mod
    from eap.config import get_settings

    old = get_settings().plugins_dir
    get_settings().__dict__["plugins_dir"] = str(tmp_path / "plugins")
    try:
        info = plugins_mod.load_plugins()
        mine = next((p for p in info if p.name == "echo-plugin"), None)
        assert mine is not None and mine.status == "loaded", mine

        listing = client.get("/api/v1/extensions/plugins", headers=AUTH).json()
        assert any(p["name"] == "echo-plugin" for p in listing)

        # 插件工具可试运行
        resp = client.post("/api/v1/extensions/tools/echo-plugin.echo/invoke", headers=AUTH,
                           json={"args": {"query": "hi"}})
        assert resp.status_code == 200
        assert "hi" in resp.json()["output"]

        # reload 幂等
        resp = client.post("/api/v1/extensions/plugins/reload", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["loaded"] >= 1
    finally:
        get_settings().__dict__["plugins_dir"] = old
        # 清理注册的工具避免污染其他用例
        from eap.runtime.workflow import _WORKFLOW_TOOLS

        _WORKFLOW_TOOLS.pop("echo-plugin.echo", None)


def test_scaffold_generates_tool_template(client: TestClient, tmp_path):
    """脚手架生成 tool 模板 → 插件加载可见。"""
    from eap.scaffold import scaffold

    written = scaffold("tool", "scaffolded-echo", str(tmp_path))
    assert any(p.endswith("manifest.py") for p in written)

    from eap import plugins as plugins_mod
    from eap.config import get_settings

    old = get_settings().plugins_dir
    get_settings().__dict__["plugins_dir"] = str(tmp_path)
    try:
        info = plugins_mod.load_plugins()
        mine = next((p for p in info if p.name == "scaffolded-echo"), None)
        assert mine is not None and mine.status == "loaded" and mine.exposes, mine
    finally:
        get_settings().__dict__["plugins_dir"] = old
        from eap.runtime.workflow import _WORKFLOW_TOOLS

        _WORKFLOW_TOOLS.pop("scaffolded-echo", None)


def test_mcp_stdio_roundtrip(client: TestClient):
    """stdio 传输端到端：注册本地手写 MCP server → validate 拉到工具清单。"""
    server_script = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("tiny-stdio")

@mcp.tool()
def greet(name: str) -> str:
    """打招呼"""
    return f"hello {name}"

mcp.run("stdio")
'''
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix="_stdio_server.py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(server_script)

    resp = client.post("/api/v1/mcp/servers", headers=AUTH, json={
        "name": "tiny-stdio", "transport": "stdio", "command": sys.executable,
        "args": [path],
    })
    assert resp.status_code == 200, resp.text

    resp = client.post("/api/v1/mcp/servers/tiny-stdio/validate", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "verified", data
    tool_names = [t["name"] for t in data["tools"]]
    assert "tiny-stdio.greet" in tool_names, tool_names

    # 工具清单出现在扩展工具列表（mcp 桥接）
    tools = client.get("/api/v1/extensions/tools", headers=AUTH).json()
    assert any(t["name"] == "mcp:tiny-stdio.tiny-stdio.greet" for t in tools)

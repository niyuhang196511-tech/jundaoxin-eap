"""扩展脚手架：python -m eap.scaffold <agent|tool|rag|mcp> <name> [--dir plugins/my-plugin]

生成可直接运行的模板项目，帮助手写扩展快速起步。
模板用 {name}/{NAME} 占位（str.replace 注入，避免 str.format 花括号转义问题）。
"""

from __future__ import annotations

import argparse
import os
import sys

TEMPLATES: dict[str, dict[str, str]] = {
    # 手写工具：register_workflow_tool + 测试
    "tool": {
        "manifest.py": '''"""手写工具插件：{name}（扩展开发体系，进程内受信代码）。"""

import json

from eap.runtime.tools import Tool
from eap.runtime.workflow import register_workflow_tool


async def handler(args_json: str) -> str:
    """工具处理器：args JSON → 结果文本（模型可读）。"""
    args = json.loads(args_json) if args_json.strip() else {}
    query = str(args.get("query", ""))
    return json.dumps({"echo": query, "hint": "替换为你的业务逻辑"}, ensure_ascii=False)


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "tool",
    "description": "{NAME}：手写工具示例（回声）",
    "register": lambda: register_workflow_tool(
        "{name}",
        lambda: Tool(
            name="{name}",
            description="{NAME}：手写工具示例",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "查询"}},
                "required": ["query"],
            },
            handler=handler,
        ),
    ),
}
''',
        "test_plugin.py": '''"""{name} 插件测试。"""


def test_tool_registers_and_runs():
    from eap.plugins import load_plugins
    from eap.runtime.workflow import _WORKFLOW_TOOLS, resolve_tool

    load_plugins()
    assert "{name}" in _WORKFLOW_TOOLS
    import asyncio

    out = asyncio.run(resolve_tool("{name}").handler('{"query": "hi"}'))
    assert "hi" in out
''',
    },
    # 手写 RAG 组件：自定义 chunker + reranker
    "rag": {
        "manifest.py": '''"""手写 RAG 组件插件：{name}（Chunker / Reranker）。"""

from eap.knowledge.components import register_chunker, register_reranker


def my_chunker(text: str, params: dict) -> list[str]:
    """自定义分块：按空行分段，段内再按 max_len 硬切。"""
    max_len = int(params.get("max_len", 500))
    out = []
    for para in (text or "").split("\\n\\n"):
        para = para.strip()
        for i in range(0, len(para), max_len):
            if para[i:i + max_len]:
                out.append(para[i:i + max_len])
    return out or [""]


def my_reranker(query: str, candidates: list[tuple[int, str]], params: dict) -> list[int]:
    """自定义重排：包含完整查询的候选优先，其余保持原序。"""
    hits = [idx for idx, content in candidates if query in content]
    rest = [idx for idx, _ in candidates if idx not in set(hits)]
    return hits + rest


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "rag",
    "description": "{NAME}：手写 RAG 组件示例",
    "register": lambda: (
        register_chunker("{name}-chunker", my_chunker, description="按段硬切分块"),
        register_reranker("{name}-reranker", my_reranker, description="完整查询命中优先"),
    ),
}
''',
    },
    # 手写 MCP Server：MCPServer（mcp 2.x SDK）
    "mcp": {
        "server.py": '''"""手写 MCP Server：{name}（stdio 传输，控制台「扩展中心 → MCP Server」添加调试）。

运行：python server.py   （经 stdio 与 EAP 通信）
依赖：随平台安装的 mcp>=2.2（mcp.server.mcpserver.MCPServer）
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("{name}")


@mcp.tool()
def echo(text: str) -> str:
    """回声示例工具——替换为你的业务工具。"""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run("stdio")
''',
    },
    # 手写 Agent：AgentApp 子类
    "agent": {
        "agent.py": '''"""手写智能体：{name}（AgentApp SDK，docs/03 §7）。"""

from eap.agents.app import AgentApp
from eap.agents.manifest import AgentManifest
from eap.agents.sdk import register_agent
from eap.schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="{name}",
    version="0.1.0",
    description="{NAME}：手写智能体示例",
    models=[{"capability": "chat", "required": True}],
    permissions=["kb.retrieve"],
)


@register_agent(MANIFEST, source="plugin")
class MyAgent(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        with self.ctx.db() as db:
            completion = await self.ctx.chat(
                db, messages=[{"role": "user", "content": request.input}],
                system="你是 {NAME} 示例助手。",
            )
            return InvokeResult(
                content=completion.result.content or "",
                steps=["answer via " + completion.record.name],
                usage={"model": completion.record.name},
            )
''',
    },
}


def _render(template: str, name: str) -> str:
    return template.replace("{name}", name).replace("{NAME}", name.replace("-", " ").replace("_", " ").title())


def scaffold(kind: str, name: str, base_dir: str) -> list[str]:
    if kind not in TEMPLATES:
        raise SystemExit(f"未知扩展类型 {kind}（可选：agent / tool / rag / mcp）")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("名称仅允许字母/数字/连字符/下划线")
    target = os.path.abspath(os.path.join(base_dir, name))
    base = os.path.abspath(base_dir)
    if os.path.commonpath([base, target]) != base:
        raise SystemExit(f"目标目录越界：{target}")
    if os.path.exists(target):
        raise SystemExit(f"目录已存在：{target}")
    os.makedirs(target)
    written = []
    for filename, template in TEMPLATES[kind].items():
        path = os.path.join(target, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_render(template, name))
        written.append(path)
    print(f"✅ 已生成 {kind} 扩展模板：")
    for p in written:
        print(f"   {p}")
    print("重启平台（或 POST /api/v1/extensions/plugins/reload）后生效。")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eap.scaffold",
                                     description="生成手写扩展模板（agent/tool/rag/mcp）")
    parser.add_argument("kind", choices=["agent", "tool", "rag", "mcp"])
    parser.add_argument("name", help="扩展名（也是插件目录名）")
    parser.add_argument("--dir", default=None,
                        help="目标根目录（默认取 EAP_PLUGINS_DIR，./plugins）")
    args = parser.parse_args()
    base = args.dir
    if base is None:
        from eap.config import get_settings

        base = get_settings().plugins_dir
    os.makedirs(base, exist_ok=True)
    scaffold(args.kind, args.name, base)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
            sys.exit(1)
        raise

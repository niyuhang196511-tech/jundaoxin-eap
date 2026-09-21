"""扩展脚手架：python -m eap.scaffold <agent|tool|rag|mcp|skill> <name> [--dir plugins/my-plugin]

生成可直接运行的模板项目，帮助手写扩展快速起步。
模板用 {name}/{NAME} 占位（str.replace 注入，避免 str.format 花括号转义问题）。
skill 模板（M34）生成 SKILL.md + scripts/demo.py + assets/README.md，
可经 skill_pkg.sign_bundle(files=...) 打包后走 /api/v1/skills/import 导入。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

TEMPLATES: dict[str, dict[str, str]] = {
    # 手写工具：register_workflow_tool + 测试
    "tool": {
        "manifest.json": '{"type": "tool", "name": "{name}", "version": "1.0.0", '
                        '"title": "{NAME}", "description": "{NAME}：手写工具示例", '
                        '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
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
        "manifest.json": '{"type": "rag", "name": "{name}", "version": "1.0.0", '
                        '"title": "{NAME}", "description": "{NAME}：手写 RAG 组件", '
                        '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
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
        "manifest.json": '{"type": "connector", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：手写 MCP Server（stdio）", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
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
        "manifest.json": '{"type": "agent", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：手写智能体", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
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
    # 手写技能：SKILL.md + scripts/assets 附件示例（M34 附件包格式，跟随技能市场惯例）
    "skill": {
        "SKILL.md": '''---
name: "{name}"
version: "1.0.0"
description: "{NAME}：手写技能示例"
permissions: []
---

## 使用说明

1. 先阅读 assets/README.md 了解附件约定（白名单扩展、大小/数量上限）。
2. 需要脚本时运行 scripts/demo.py——技能包只分发不执行，运行一律经
   M33 沙箱 script_tool（导入后由平台按需调用，本文件不会被自动运行）。
''',
        "scripts/demo.py": '''"""{name} 示例脚本：stdin JSON → 处理 → stdout JSON。

技能包中的脚本仅随包分发（M34：逐文件 sha256 清单随签名覆盖，导入端逐一复核），
执行属 M33 沙箱 script_tool 范畴，平台不会自动运行本文件。替换为你的业务逻辑。
"""

import json
import sys


def main() -> None:
    """读取 stdin JSON，回显并附提示（确定性示例，便于联调）。"""
    raw = sys.stdin.read() or "{}"
    data = json.loads(raw)
    json.dump({"echo": data, "hint": "替换为你的业务逻辑"}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
''',
        "assets/README.md": '''# {NAME} 技能附件

- 本目录存放技能运行所需的静态资源；允许的扩展名：
  .png / .jpg / .svg / .csv / .json / .md / .txt（EAP-8104 白名单）。
- 打包时逐文件计算 sha256 写入签名清单（随包签名覆盖），导入端逐一复核，
  清单与实际不符即拒绝（EAP-8104）。
- 上限：单文件 ≤1MB、总文件数 ≤10、总量 ≤5MB。替换为你的资源文件。
''',
    },
}


def _render(template: str, name: str) -> str:
    return template.replace("{name}", name).replace("{NAME}", name.replace("-", " ").replace("_", " ").title())


def pack(source_dir: str, out_path: str) -> str:
    """目录 → .eapext zip（v0.7 开发者接入生命周期：打包 → 安装端点安装）。"""
    from .runtime.bundles import pack_bundle

    return pack_bundle(source_dir, out_path)


def scaffold(kind: str, name: str, base_dir: str) -> list[str]:
    if kind not in TEMPLATES:
        raise SystemExit(f"未知扩展类型 {kind}（可选：agent / tool / rag / mcp / skill）")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("名称仅允许字母/数字/连字符/下划线")
    base = Path(base_dir).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base):
        raise SystemExit(f"目标目录越界：{target}")
    if target.exists():
        raise SystemExit(f"目录已存在：{target}")
    target.mkdir(parents=True)
    written = []
    for filename, template in TEMPLATES[kind].items():
        path = target / filename
        path.parent.mkdir(parents=True, exist_ok=True)  # M34 skill 模板含 scripts/ assets/ 子目录
        path.write_text(_render(template, name), encoding="utf-8", newline="\n")
        written.append(str(path))
    print(f"✅ 已生成 {kind} 扩展模板：")
    for p in written:
        print(f"   {p}")
    if kind == "skill":
        print("打包：skill_pkg.sign_bundle(skill, files=[...]) → POST /api/v1/skills/import 导入。")
    else:
        print("重启平台（或 POST /api/v1/extensions/plugins/reload）后生效。")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eap.scaffold",
                                     description="生成手写扩展模板（agent/tool/rag/mcp/skill）或打包 .eapext")
    sub = parser.add_subparsers(dest="command")
    gen = sub.add_parser("generate", help="生成手写扩展模板")
    gen.add_argument("kind", choices=["agent", "tool", "rag", "mcp", "skill"])
    gen.add_argument("name", help="扩展名（也是插件目录名）")
    gen.add_argument("--dir", default=None,
                     help="目标根目录（默认取 EAP_PLUGINS_DIR，./plugins）")
    pack_p = sub.add_parser("pack", help="扩展目录 → .eapext 安装包")
    pack_p.add_argument("source", help="扩展目录")
    pack_p.add_argument("out", help="输出 .eapext 路径")
    # 兼容旧用法：python -m eap.scaffold <kind> <name> [--dir ...]
    args, extra = parser.parse_known_args()
    if args.command is None and extra:
        # 旧式位置参数：<kind> <name>
        kind, name = extra[0], extra[1] if len(extra) > 1 else ""
        base = args.dir if False else None
        from eap.config import get_settings

        base = (args.dir if hasattr(args, "dir") else None) or get_settings().plugins_dir
        os.makedirs(base, exist_ok=True)
        scaffold(kind, name, base)
        return
    if args.command == "pack":
        print(pack(args.source, args.out))
        return
    from eap.config import get_settings

    base = args.dir or get_settings().plugins_dir
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

"""扩展脚手架：python -m eap.scaffold <agent|tool|rag|mcp|skill|workflow-node|model-provider|connector|ui-component> <name> [--dir plugins/my-plugin]

生成可直接运行的模板项目，帮助手写扩展快速起步。
模板用 {name}/{NAME} 占位（str.replace 注入，避免 str.format 花括号转义问题）。
skill 模板（M34）生成 SKILL.md + scripts/demo.py + assets/README.md，
可经 skill_pkg.sign_bundle(files=...) 打包后走 /api/v1/skills/import 导入。
M48-B 补齐 ext/sdk.py 五类契约中缺脚手架的四类：
workflow-node / model-provider / connector / ui-component（模板 = manifest + 入口 + README）。
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
    # 手写工作流节点（M48-B，v0.7-Workflow Node SDK）：自定义节点类型进 DSL 合法集合
    "workflow-node": {
        "manifest.json": '{"type": "workflow_node", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：自定义工作流节点", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
        "manifest.py": '''"""手写工作流节点插件：{name}（Workflow Node SDK，docs/14-extension-sdk.md §二.3）。

注册后 kind "{name}" 进入 DSL Step.type 合法集合（parallel/loop 体内不可用），
工作流步骤直接写 {"type": "{name}", "id": "..."}。
executor 签名固定（平台 await 调用，须为 async）：
    executor(step, ctx, db, state, invoke_input) -> str（输出供下游节点取用）
"""

from eap.ext import register_workflow_node


async def executor(step, ctx, db, state, invoke_input) -> str:
    """自定义节点执行体——替换为你的业务逻辑。

    - step：DSL Step 对象（step.id / step.system / step.tool_args 等字段）
    - state.variables：上游节点输出（{变量名: 值}）
    - invoke_input：本次工作流调用的原始输入文本
    """
    upstream = str(invoke_input or "")
    return f"[{step.id}] {NAME}: {upstream.upper()}"


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "workflow_node",
    "description": "{NAME}：自定义工作流节点示例",
    "register": lambda: register_workflow_node("{name}", executor),
}
''',
        "README.md": '''# {NAME}（自定义工作流节点）

Workflow Node SDK 骨架：注册一个自定义节点类型（executor + 声明）。

## 文件
- manifest.json：统一扩展 Manifest（type=workflow_node）
- manifest.py：executor（节点执行体）+ EAP_PLUGIN 注册声明（平台插件入口）

## 加载
把本目录拷贝到插件目录（默认 ./plugins，或 EAP_PLUGINS_DIR）后重启平台，
或 POST /api/v1/extensions/plugins/reload 热加载。

## 验证
工作流 DSL 里直接使用该节点类型（自定义 kind 通过 Step.type 校验）：

    {"steps": [{"id": "go", "type": "{name}"}, {"id": "say", "type": "llm"}]}

注意：parallel/loop 体内不可用（保确定性重放）。
''',
    },
    # 手写模型供应商适配（M48-B，v0.7-Model Provider SDK）：complete/stream_complete 协议
    "model-provider": {
        "manifest.json": '{"type": "model_provider", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：自定义模型供应商适配", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
        "manifest.py": '''"""手写模型供应商适配：{name}（Model Provider SDK，docs/14-extension-sdk.md §二.4）。

注册后把模型登记（模型中心）的 provider 字段设为 "{name}"，路由器经
get_provider("{name}") 解析到本适配（modelhub/router.py：同步与流式共用此解析）。
协议见 eap.modelhub.providers.Provider：complete(...) -> LLMResult +
stream_complete(...) -> AsyncIterator[str]；调用失败抛 ProviderError 走降级链。
"""

from collections.abc import AsyncIterator

from eap.ext import register_provider
from eap.modelhub.providers import LLMResult, ProviderError


class MyProvider:
    """供应商适配：record 为模型登记记录（base_url/api_key/remote_model/name 等）。

    api_key 存储态可能是 enc1: 前缀密文，用 eap.security_crypto.decrypt_secret 解密。
    """

    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None,
                       temperature: float,
                       response_schema: dict | None = None) -> LLMResult:
        """补全：调用你的后端 API → 组装 LLMResult。替换占位为真实请求。"""
        import time

        t0 = time.monotonic()
        prompt = next((m.get("content") or "" for m in reversed(messages)
                       if m.get("role") == "user"), "")
        try:
            # TODO: 在此发起真实 HTTP 调用（httpx.AsyncClient POST record.base_url）
            content = f"[{record.name}] echo: {prompt}"
        except Exception as e:  # 网络等失败 → 路由器捕获并走降级链
            raise ProviderError(f"{record.name}: {e}") from e
        return LLMResult(
            content=content,
            tokens_in=len(str(messages)) // 4, tokens_out=len(content) // 4,
            model=record.name, latency_ms=int((time.monotonic() - t0) * 1000),
        )

    async def stream_complete(self, *, record, messages: list[dict],
                              temperature: float) -> AsyncIterator[str]:
        """token 级流式：逐段 yield 文本片段（SSE 场景替换为真流式解析）。"""
        result = await self.complete(record=record, messages=messages, tools=None,
                                     temperature=temperature)
        for piece in (result.content or "").split(" "):
            yield piece + " "

    async def health(self) -> dict:
        """供应商健康自检（平台不自动调用；管理端排障/扩展侧运维用）。"""
        return {"provider": "{name}", "ok": True}


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "model_provider",
    "description": "{NAME}：自定义模型供应商适配示例",
    "register": lambda: register_provider("{name}", MyProvider()),
}
''',
        "README.md": '''# {NAME}（自定义模型供应商适配）

Model Provider SDK 骨架：complete / stream_complete / health 三件套。

## 文件
- manifest.json：统一扩展 Manifest（type=model_provider）
- manifest.py：MyProvider（协议实现）+ EAP_PLUGIN 注册声明

## 加载
拷贝到插件目录（默认 ./plugins）后重启平台，或热加载：
POST /api/v1/extensions/plugins/reload。

## 验证
1. 注册后 `get_provider("{name}")` 即解析到本适配（eap.modelhub.providers）。
2. 在「模型中心」登记模型时把 provider 填为 "{name}"、base_url 指向你的服务，
   聊天/工作流路由即经本适配出话；失败抛 ProviderError 自动走降级链。

get_provider 解析顺序：自定义注册表（register_provider）→ mock → openai_compat。
''',
    },
    # 手写连接器类型（M48-B，v0.7-Connector SDK）：endpoints → 工具集
    "connector": {
        "manifest.json": '{"type": "connector", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：自定义连接器类型", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
        "manifest.py": '''"""手写连接器类型插件：{name}（Connector SDK，docs/14-extension-sdk.md §二.5）。

注册后，管理端登记 kind="{name}" 的连接器（base_url + endpoints）时，
工具构建走本文件 factory：factory(ConnectorRecord) -> list[Tool]。
Tool.name 约定与内置 rest 一致：endpoint["tool_name"] 或
f"conn.{record.name}.{endpoint name}"（endpoint_tool 按名取用）。
"""

import json

from eap.ext import register_connector_kind
from eap.runtime.tools import Tool


def _params_schema(endpoint: dict) -> dict:
    """端点 params 声明 → JSON Schema（与内置 rest 连接器同形）。"""
    params = endpoint.get("params") or []
    props = {p.get("name", "arg"): {"type": p.get("type", "string")}
             for p in params if isinstance(p, dict)}
    return {"type": "object", "properties": props}


def factory(record) -> list[Tool]:
    """把连接器的每个 endpoint 包装为平台 Tool（handler 内替换为真实调用）。"""
    tools: list[Tool] = []
    for endpoint in (record.endpoints or []):
        name = endpoint.get("tool_name") or f"conn.{record.name}.{endpoint.get('name', '')}"

        def handler(args_json: str, _record=record, _endpoint=endpoint) -> str:
            args = json.loads(args_json) if args_json.strip() else {}
            # TODO: 真实调用——httpx.AsyncClient 请求 _record.base_url + _endpoint["path"]
            # 鉴权占位：请求头名取 _record.header_name，凭证取 _record.api_key
            #（enc1: 前缀密文经 eap.security_crypto.decrypt_secret 解密；OAuth 托管同理）。
            return json.dumps({
                "connector": _record.name,
                "endpoint": _endpoint.get("name", ""),
                "method": _endpoint.get("method", "GET"),
                "auth_header": _record.header_name or "Authorization",
                "args": args,
                "hint": "替换为真实 endpoint 调用",
            }, ensure_ascii=False)

        tools.append(Tool(
            name=name,
            description=endpoint.get("description") or f"连接器 {record.name} 端点 {name}",
            parameters=_params_schema(endpoint),
            handler=handler,
            requires_approval=bool(endpoint.get("requires_approval")),
        ))
    return tools


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "connector",
    "description": "{NAME}：自定义连接器类型示例",
    "register": lambda: register_connector_kind("{name}", factory),
}
''',
        "README.md": '''# {NAME}（自定义连接器类型）

Connector SDK 骨架：超出 rest / mock-erp / sql 之外的连接器形态
（如 SDK 化的 SaaS 客户端）。

## 文件
- manifest.json：统一扩展 Manifest（type=connector）
- manifest.py：factory（endpoints → 平台工具集，含鉴权占位）+ EAP_PLUGIN 注册声明

## 加载
拷贝到插件目录（默认 ./plugins）后重启平台，或热加载：
POST /api/v1/extensions/plugins/reload。

## 验证
1. 管理端「连接器」新建连接器：kind 填 "{name}"，base_url / api_key /
   endpoints（[{tool_name, method, path, description, params}]）按你的系统登记。
2. 保存后连接器端点即包装为平台工具（工具名 = endpoint.tool_name），
   智能体工具池 / Action / UI 按钮可直接调用；handler 内替换为真实 HTTP 调用。
''',
    },
    # 手写 UI 交互组件（M48-B，v0.7-UI SDK）：组合式 UISchema 片段
    "ui-component": {
        "manifest.json": '{"type": "ui", "name": "{name}", "version": "1.0.0", '
                         '"title": "{NAME}", "description": "{NAME}：组合式自定义交互组件", '
                         '"runtime": {"min_version": "0.7.0"}, "permissions": []}',
        "manifest.py": '''"""手写 UI 交互组件插件：{name}（UI SDK，docs/14-extension-sdk.md §二.6）。

组件 = 组合式 UISchema 片段（必须含 fields 数组，否则注册即拒绝）。
Agent 的 interaction_schema / output_schema 以
{"type": "custom", "component": "{name}"} 引用：
前端以内联子表单渲染组件声明的 fields，无需任何前端代码。
"""

from eap.ext import register_ui_component

SCHEMA_FRAGMENT = {
    "label": "{NAME} 选择器",
    "fields": [
        {"type": "select", "id": "warehouse", "label": "仓库", "required": True,
         "options": [{"label": "一号仓", "value": "w1"},
                     {"label": "二号仓", "value": "w2"}]},
        {"type": "number", "id": "qty", "label": "数量", "required": True},
    ],
}


EAP_PLUGIN = {
    "name": "{name}",
    "kind": "ui",
    "description": "{NAME}：组合式自定义交互组件示例",
    "register": lambda: register_ui_component("{name}", SCHEMA_FRAGMENT),
}
''',
        "README.md": '''# {NAME}（组合式自定义交互组件）

UI SDK（MVP）骨架：组件即声明式 UISchema 片段，不含任何前端代码。

## 文件
- manifest.json：统一扩展 Manifest（type=ui）
- manifest.py：SCHEMA_FRAGMENT（fields 内联子表单声明）+ EAP_PLUGIN 注册声明

## 加载
拷贝到插件目录（默认 ./plugins）后重启平台，或热加载：
POST /api/v1/extensions/plugins/reload。

## 验证
Agent 声明 interaction_schema 时以 {"type": "custom", "component": "{name}"}
引用本组件；用户输入挂起（WAITING_INPUT）后提交的表单值经 /tasks/{id}/interact
回传续跑。fields 支持 select / number / text 等组合式类型（见 interaction schema 校验）。
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
        raise SystemExit(f"未知扩展类型 {kind}（可选：agent / tool / rag / mcp / skill / "
                         "workflow-node / model-provider / connector / ui-component）")
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
                                     description="生成手写扩展模板（agent/tool/rag/mcp/skill/"
                                                 "workflow-node/model-provider/connector/ui-component）"
                                                 "或打包 .eapext")
    sub = parser.add_subparsers(dest="command")
    gen = sub.add_parser("generate", help="生成手写扩展模板")
    gen.add_argument("kind", choices=["agent", "tool", "rag", "mcp", "skill",
                                      "workflow-node", "model-provider", "connector", "ui-component"])
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

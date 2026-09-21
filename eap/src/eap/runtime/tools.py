"""Agent 工具注册表：OpenAI function-calling 格式（docs/03 §1.2 工具调用协议）。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class Tool:
    """工具 = 名称 + JSON Schema 参数 + 异步处理器（args JSON → 结果文本）。

    治理元数据（v0.6-①）：risk_level 参与策略审批阈值判定；timeout_s 强制执行时限。
    沙箱元数据（M33 任务组 P2）：runtime=script 的工具经 SandboxRunner 子进程
    执行外部脚本（script_path），进程内实现无法沙箱化（tool-sandbox 策略联动）。
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[str], Awaitable[str]]
    emits_citations: bool = False  # 循环器会尝试从返回 JSON 中提取 citations
    requires_approval: bool = False  # HITL：执行前需人工审批（docs/03 §1.2）
    risk_level: str = "low"  # low | medium | high（策略 tool-risk-approval 阈值判定）
    timeout_s: float = 30.0  # 单次执行时限（超时按工具失败回注模型）
    runtime: str = "inproc"  # inproc | script（M33：script = 外部脚本经沙箱子进程执行）
    script_path: str | None = None  # runtime=script 时的脚本文件路径（平台进程视角）

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_tools(*tools: Tool) -> list[Tool]:
    seen: dict[str, Tool] = {}
    for t in tools:
        if t.name in seen:
            raise ValueError(f"工具重名: {t.name}")
        seen[t.name] = t
    return list(seen.values())


def find_tool(tools: list[Tool], name: str) -> Tool | None:
    return next((t for t in tools if t.name == name), None)


async def run_script_in_sandbox(script_path: str, args_json: str, *, timeout_s: float) -> str:
    """脚本工具统一执行入口（M33）：强制经 SandboxRunner（工厂 handler 与
    loop 沙箱路由共用同一函数，防止绕过沙箱直连脚本）。

    协议：args JSON 从 stdin 进、单行 JSON 从 stdout 出；脚本失败（非零退出/
    超时/启动失败）回注 {"error": ...}，与进程内工具失败语义一致。
    """
    from .sandbox import get_runner

    result = await get_runner().run_async(script_path, args_json if args_json else "{}",
                                          timeout_s=timeout_s)
    if result.get("ok"):
        out = (result.get("stdout") or "").strip()
        return out if out else json.dumps({"ok": True}, ensure_ascii=False)
    if result.get("timed_out"):
        return json.dumps({"error": f"脚本执行超时（沙箱 timeout_s={timeout_s}）"},
                          ensure_ascii=False)
    stderr_tail = (result.get("stderr") or "").strip().splitlines()
    detail = stderr_tail[-1] if stderr_tail else f"exit_code={result.get('exit_code')}"
    return json.dumps({"error": f"脚本工具执行失败: {detail}"}, ensure_ascii=False)


def script_tool(name: str, description: str, parameters: dict, script_path: str, *,
                risk_level: str = "medium", timeout_s: float = 60.0,
                requires_approval: bool = False, emits_citations: bool = False) -> Tool:
    """脚本工具工厂（M33 任务组 P2）：外部脚本经沙箱子进程受限执行。

    脚本约定：从 stdin 读单行 JSON 入参、向 stdout 写单行 JSON 结果；
    沙箱根 = 一次性临时目录（cwd），环境变量已裁剪（详见 runtime/sandbox.py）。
    """
    path = str(script_path)

    async def _handler(args: str) -> str:
        return await run_script_in_sandbox(path, args, timeout_s=timeout_s)

    return Tool(name=name, description=description, parameters=parameters, handler=_handler,
                risk_level=risk_level, timeout_s=timeout_s, requires_approval=requires_approval,
                emits_citations=emits_citations, runtime="script", script_path=path)

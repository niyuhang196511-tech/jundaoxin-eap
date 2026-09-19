"""Agent 工具注册表：OpenAI function-calling 格式（docs/03 §1.2 工具调用协议）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class Tool:
    """工具 = 名称 + JSON Schema 参数 + 异步处理器（args JSON → 结果文本）。

    治理元数据（v0.6-①）：risk_level 参与策略审批阈值判定；timeout_s 强制执行时限。
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[str], Awaitable[str]]
    emits_citations: bool = False  # 循环器会尝试从返回 JSON 中提取 citations
    requires_approval: bool = False  # HITL：执行前需人工审批（docs/03 §1.2）
    risk_level: str = "low"  # low | medium | high（策略 tool-risk-approval 阈值判定）
    timeout_s: float = 30.0  # 单次执行时限（超时按工具失败回注模型）

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

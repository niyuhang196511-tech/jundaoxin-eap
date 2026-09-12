"""供应商适配器：统一 LLMResult。

M1 简化：SSE 在 API 层以「整段结果分片」下发（线上协议正确），
真实 token 级流式（httpx stream）在 M2 接入 —— 见 docs/09 路线。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


def approx_tokens(text: str) -> int:
    """粗略 token 估算（生产应使用分词器精确计量，见 docs/08 §4）。"""
    return max(1, len(text or "") // 4)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串（OpenAI wire 格式）


@dataclass
class LLMResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_ms: int = 0


class ProviderError(Exception):
    """供应商调用失败——由路由器捕获并走降级链。"""


class Provider(Protocol):
    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float) -> LLMResult: ...


class MockProvider:
    """确定性 mock（离线可用）：

    - 有工具且对话中尚无工具结果 → 发起第一次工具调用（取第一个工具）
    - 已有工具结果 → 汇总工具输出作答
    - 无工具 → 确定性回声回答
    """

    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float) -> LLMResult:
        t0 = time.monotonic()
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        last_user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")

        if tools and not has_tool_result:
            fn = tools[0]["function"]
            args = json.dumps({"query": last_user[:60].replace('"', "'")}, ensure_ascii=False)
            tc = ToolCall(id="call_" + uuid.uuid4().hex[:8], name=fn["name"], arguments=args)
            return self._result(record, None, [tc], messages, t0)

        if has_tool_result:
            tool_msg = next(m for m in reversed(messages) if m.get("role") == "tool")
            snippet = (tool_msg.get("content") or "")[:300]
            content = f"[mock-llm] 根据工具返回：{snippet}……以上为离线 mock 汇总回答。"
        else:
            content = (
                f"[mock-llm] 收到：{last_user}。这是离线确定性回复。"
                "配置 EAP_OPENAI_BASE_URL / EAP_OPENAI_API_KEY 后，模型中心将优先路由到真实供应商。"
            )
        return self._result(record, content, [], messages, t0)

    @staticmethod
    def _result(record, content, tool_calls, messages, t0) -> LLMResult:
        tin = sum(approx_tokens(str(m.get("content", ""))) for m in messages)
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            tokens_in=tin,
            tokens_out=approx_tokens(content or "") + 10,
            model=record.name,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


class OpenAICompatProvider:
    """OpenAI 兼容供应商（OpenAI / DeepSeek / 通义 / vLLM 等，docs/04 §2.1）。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=60.0)

    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float) -> LLMResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = {
            "model": record.remote_model or record.name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = await self._client.post(
                f"{record.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {record.api_key}"},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.StreamError) as e:
            raise ProviderError(f"{record.name}: {e}") from e

        data = resp.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        tool_calls = [
            ToolCall(id=t["id"], name=t["function"]["name"], arguments=t["function"].get("arguments") or "{}")
            for t in (msg.get("tool_calls") or [])
        ]
        return LLMResult(
            content=msg.get("content"),
            tool_calls=tool_calls,
            tokens_in=usage.get("prompt_tokens", approx_tokens(json.dumps(messages, ensure_ascii=False))),
            tokens_out=usage.get("completion_tokens", approx_tokens(msg.get("content") or "")),
            model=record.name,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


def get_provider(provider_name: str) -> Provider:
    if provider_name == "mock":
        return _MOCK
    if provider_name == "openai_compat":
        return _OPENAI
    raise ProviderError(f"未知 provider: {provider_name}")


_MOCK = MockProvider()
_OPENAI = OpenAICompatProvider()

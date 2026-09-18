"""供应商适配器：统一 LLMResult + token 级流式（docs/04 §2.1）。

流式：stream_complete 逐 token 产出文本增量；无工具场景直接流式，
带工具调用时仍走整段 complete（工具调用增量组装无收益）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


def approx_tokens(text: str) -> int:
    """token 估算：tiktoken 可用时按 cl100k_base 精确计数，否则 len//4 粗估（docs/08 §4）。"""
    if not text:
        return 0
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is False:  # 惰性探测一次
        try:
            import tiktoken

            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TIKTOKEN_ENC = None
    if _TIKTOKEN_ENC is not None:
        return max(1, len(_TIKTOKEN_ENC.encode(text)))
    return max(1, len(text) // 4)


_TIKTOKEN_ENC: object | None | bool = False  # False=未探测 / None=不可用 / Encoding 实例


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
    data: dict | None = None  # 结构化输出（v0.5）：schema 校验通过的 JSON 对象


def _bearer(stored: str | None) -> str | None:
    """静态存储的 api_key → 请求用明文（enc1: 密文解密；明文兼容期原样）。"""
    if not stored:
        return stored
    from ..security_crypto import decrypt_secret

    return decrypt_secret(stored)


class ProviderError(Exception):
    """供应商调用失败——由路由器捕获并走降级链。"""


class Provider(Protocol):
    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float,
                       response_schema: dict | None = None) -> LLMResult: ...
    def stream_complete(self, *, record, messages: list[dict], temperature: float) -> AsyncIterator[str]: ...


def synthesize_from_schema(schema: dict, depth: int = 0):
    """按 JSON Schema 确定性合成最小合规值（离线 mock 的结构化输出合同）。

    required 字段必出；enum/const 取首项；object 取全部属性（深度 ≤3）；
    string → "mock-<名称>"，integer/number → 1，boolean → true，array → 单元素。
    """
    if depth > 3:
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or props.keys())
        return {k: synthesize_from_schema(v, depth + 1) for k, v in props.items()
                if k in required or depth < 2}
    if t == "array":
        return [synthesize_from_schema(schema.get("items") or {"type": "string"}, depth + 1)]
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "null":
        return None
    label = str(schema.get("title") or schema.get("description") or "value")
    return "mock-" + "".join(ch if ch.isalnum() else "-" for ch in label.lower())[:24]


class MockProvider:
    """确定性 mock（离线可用）：

    - 有工具且对话中尚无工具结果 → 发起第一次工具调用（取第一个工具）
    - 已有工具结果 → 汇总工具输出作答
    - 无工具 → 确定性回声回答
    """

    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float,
                       response_schema: dict | None = None) -> LLMResult:
        t0 = time.monotonic()
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        last_user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")

        # LLM 裁判分支（EAP-JUDGE 标记由评测中心 judge prompt 携带，真实模型视为普通上下文）：
        # 离线确定性裁判——用例输入含「期望不通过」则 FAIL，否则 PASS（docs/08 §3 离线测试约定）
        if any("EAP-JUDGE" in str(m.get("content") or "") for m in messages):
            passed = "期望不通过" not in last_user
            verdict = json.dumps({"passed": passed, "reason": "mock 裁判（离线确定性）"}, ensure_ascii=False)
            return self._result(record, verdict, [], messages, t0)

        # 图谱抽取分支（EAP-GRAPH 标记由 knowledge/graph.py 抽取 prompt 携带）：
        # 离线确定性抽取——实体取【文本】标记之后的 ASCII 词、相邻建关系
        # （真实模型按语义抽取）
        if any("EAP-GRAPH" in str(m.get("content") or "") for m in messages):
            import re as _re

            tail = last_user.split("【文本】")[-1]
            words = list(dict.fromkeys(_re.findall(r"[a-zA-Z0-9]{2,}", tail)))[:4]
            graph = {"entities": words,
                     "relations": [[words[i], words[i + 1], "co"] for i in range(len(words) - 1)]}
            return self._result(record, json.dumps(graph, ensure_ascii=False), [], messages, t0)

        # 结构化输出（v0.5）：按 JSON Schema 确定性合成（离线测试的 mock 合同）
        if response_schema is not None and (not tools or has_tool_result):
            data = synthesize_from_schema(response_schema)
            return self._result(record, json.dumps(data, ensure_ascii=False), [], messages, t0, data=data)

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

    async def stream_complete(self, *, record, messages: list[dict], temperature: float) -> AsyncIterator[str]:
        """离线确定性流式：按词切分（每词一帧，10ms 间隔），语义与 complete 一致。"""
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        last_user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
        if has_tool_result:
            tool_msg = next(m for m in reversed(messages) if m.get("role") == "tool")
            snippet = (tool_msg.get("content") or "")[:300]
            content = f"[mock-llm] 根据工具返回：{snippet}……以上为离线 mock 汇总回答。"
        else:
            content = (
                f"[mock-llm] 收到：{last_user}。这是离线确定性回复。"
                "配置 EAP_OPENAI_BASE_URL / EAP_OPENAI_API_KEY 后，模型中心将优先路由到真实供应商。"
            )
        piece = ""
        for ch in content:
            piece += ch
            if ch in (" ", "，", "。", "；", "\n") or len(piece) >= 6:
                yield piece
                piece = ""
                await asyncio.sleep(0.01)
        if piece:
            yield piece

    @staticmethod
    def _result(record, content, tool_calls, messages, t0, data: dict | None = None) -> LLMResult:
        tin = sum(approx_tokens(str(m.get("content", ""))) for m in messages)
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            tokens_in=tin,
            tokens_out=approx_tokens(content or "") + 10,
            model=record.name,
            latency_ms=int((time.monotonic() - t0) * 1000),
            data=data,
        )


class OpenAICompatProvider:
    """OpenAI 兼容供应商（OpenAI / DeepSeek / 通义 / vLLM 等，docs/04 §2.1）。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=60.0)

    async def complete(self, *, record, messages: list[dict], tools: list[dict] | None, temperature: float,
                       response_schema: dict | None = None) -> LLMResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = {
            "model": record.remote_model or record.name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if response_schema is not None and not tools:
            # 结构化输出（v0.5）：优先结构化解码；供应商不支持时由 hub 的 prompt 注入路径兜底
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "eap_response", "strict": True, "schema": response_schema},
            }
        try:
            resp = await self._client.post(
                f"{record.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {_bearer(record.api_key)}"},
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

    async def stream_complete(self, *, record, messages: list[dict], temperature: float) -> AsyncIterator[str]:
        """真 token 级流式：httpx stream + SSE delta.content 逐段产出。"""
        payload: dict[str, Any] = {
            "model": record.remote_model or record.name,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        try:
            async with self._client.stream(
                "POST",
                f"{record.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {_bearer(record.api_key)}"},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
        except (httpx.HTTPError, httpx.StreamError) as e:
            raise ProviderError(f"{record.name}: {e}") from e


def get_provider(provider_name: str) -> Provider:
    if provider_name == "mock":
        return _MOCK
    if provider_name == "openai_compat":
        return _OPENAI
    raise ProviderError(f"未知 provider: {provider_name}")


_MOCK = MockProvider()
_OPENAI = OpenAICompatProvider()

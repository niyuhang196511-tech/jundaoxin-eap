"""模型中心路由器：能力路由 + 降级链 + 策略钩子（docs/04 §2.1-2.2 的 M1 版）。

Policy Engine 的 M1 钩子位：route_policies() 可按租户/数据分级扩展过滤（docs/06 §2）。
结构化输出（v0.5）：response_schema 下发时注入格式指令 → 解析 → jsonschema 校验 →
失败带错误反馈重试一次 → 仍失败回退纯文本（data=None）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ModelRecord
from .providers import ProviderError, get_provider


@dataclass
class Completion:
    result: object  # LLMResult
    record: ModelRecord


class ModelHub:
    def chain_for(self, db: Session, capability: str = "chat", prefer: str | None = None) -> list[ModelRecord]:
        """按能力过滤、按 priority 升序（小者优先）、prefer 指定模型置顶。"""
        records = [
            r for r in db.scalars(select(ModelRecord).where(ModelRecord.enabled == True)).all()  # noqa: E712
            if capability in (r.capabilities or [])
        ]
        records.sort(key=lambda r: r.priority)
        if prefer and prefer != "auto":
            matched = [r for r in records if r.name == prefer]
            rest = [r for r in records if r.name != prefer]
            records = matched + rest
        return records

    async def complete(
        self,
        db: Session,
        messages: list[dict],
        *,
        capability: str = "chat",
        tools: list[dict] | None = None,
        prefer: str | None = None,
        temperature: float = 0.7,
        response_schema: dict | None = None,
        only_prefer: bool = False,
    ) -> Completion:
        """遍历降级链：供应商失败自动切换下一个（docs/02 §5 ①）。每次尝试一个 LLM span。

        response_schema（v0.5 结构化输出）：注入格式指令 → 解析 + jsonschema 校验 →
        失败带错误反馈重试一次 → 仍失败回退纯文本（result.data=None）。

        only_prefer（M45-B）：钉死 prefer 指定模型直连，链上其余模型全部剔除、失败
        不降级（直抛 ProviderError）——模型直评等「必须测到目标模型本身」的场景用；
        prefer 缺省/auto 时该参数无效果。
        """
        if response_schema is not None:
            messages = _with_schema_instruction(messages, response_schema)
        completion = await self._complete_chain(
            db, messages, capability=capability, tools=tools, prefer=prefer,
            temperature=temperature, response_schema=response_schema, only_prefer=only_prefer,
        )
        if response_schema is None or completion.result.tool_calls:
            return completion
        data, err = parse_structured(completion.result.content, response_schema)
        if data is not None:
            completion.result.data = data
            return completion
        # 重试一次：回注错误反馈（不带 tools 重新补全，模型只需修正格式）
        retry_messages = [*messages,
                          {"role": "assistant", "content": completion.result.content or ""},
                          {"role": "user", "content": f"上次输出不符合 JSON Schema（{err}）。"
                                                      "请只输出符合 schema 的 JSON 对象，不要包含其他文字。"}]
        retry = await self._complete_chain(
            db, retry_messages, capability=capability, tools=None, prefer=prefer,
            temperature=temperature, response_schema=response_schema, only_prefer=only_prefer,
        )
        data, _err = parse_structured(retry.result.content, response_schema)
        if data is not None:
            retry.result.data = data
            retry.result.tokens_in += completion.result.tokens_in
            retry.result.tokens_out += completion.result.tokens_out
            return retry
        return completion  # 回退纯文本（data=None）

    async def _complete_chain(
        self,
        db: Session,
        messages: list[dict],
        *,
        capability: str = "chat",
        tools: list[dict] | None = None,
        prefer: str | None = None,
        temperature: float = 0.7,
        response_schema: dict | None = None,
        only_prefer: bool = False,
    ) -> Completion:
        """沿降级链执行一次补全（供应商失败自动切换下一个）。

        only_prefer（M45-B）：prefer 显式指定时把链钉死为该模型一个元素——直评等
        场景「必须测到目标模型本身」，供应商失败也不允许落到链上其他模型。
        """
        if prefer is None:
            from ..runtime.canary import current_model_override

            prefer = current_model_override()  # canary 灰度覆盖（显式 prefer 优先）
        from ..observability.tracing import enabled as otel_enabled, tracer
        from ..runtime.policy import apply_eval_gate, check_prompt, enforce_chain

        check_prompt(db, messages)  # Policy Engine：单次调用 prompt 上限
        chain = enforce_chain(db, self.chain_for(db, capability=capability, prefer=prefer))
        if only_prefer and prefer and prefer != "auto":
            chain = [r for r in chain if r.name == prefer]
            if not chain:
                raise ProviderError(
                    f"指定模型 {prefer} 不存在、未启用或不具备 [{capability}] 能力（直连模式不降级）")
        if not chain:
            raise ProviderError(f"没有启用 [{capability}] 能力的模型，请先在模型中心注册")
        # 熔断过滤（M31 任务组 F）：open 的模型从候选链剔除，自然落到降级链下一个
        from ..observability.gateway import get_breaker

        breaker = get_breaker()
        chain = [r for r in chain if breaker.allow(r.name)]
        if not chain:
            raise ProviderError(f"[{capability}] 链上模型均处于熔断状态，请稍后重试")
        # 评测门禁（M42-B，docs/10 遗留项）：未过评测的模型从候选链剔除，
        # 自然落到降级链下一个（与熔断过滤同位同模式），剔除明细落审计
        chain, gate_blocked = apply_eval_gate(db, chain)
        for item in gate_blocked:
            from ..observability import audit

            audit.record("model.eval_gate.blocked", target=item["model"],
                         detail={"reason": item["reason"], "policy": item["policy"],
                                 "capability": capability})
        if not chain:
            raise ProviderError(f"[{capability}] 链上模型均未通过评测门禁，请先在评测中心通过评测")
        errors: list[str] = []
        for record in chain:
            span_cm = tracer().start_as_current_span(
                f"llm.complete {record.name}") if otel_enabled() else None
            span = span_cm.__enter__() if span_cm is not None else None
            if span is not None:
                span.set_attribute("llm.model", record.name)
                span.set_attribute("llm.provider", record.provider)
            try:
                provider = get_provider(record.provider)
                result = await provider.complete(record=record, messages=messages, tools=tools,
                                                 temperature=temperature, response_schema=response_schema)
                breaker.record_success(record.name)
                if span is not None:
                    span.set_attribute("llm.tokens_out", result.tokens_out)
                    span.set_attribute("llm.latency_ms", result.latency_ms)
                return Completion(result=result, record=record)
            except ProviderError as e:
                breaker.record_failure(record.name)
                errors.append(str(e))
                if span is not None:
                    span.record_exception(e)
            except Exception as e:
                breaker.record_failure(record.name)  # 非供应商异常同样计入熔断（防半开探测泄漏）
                raise
            finally:
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)
        raise ProviderError("所有模型均失败（降级链耗尽）: " + " | ".join(errors))

    async def stream(
        self,
        db: Session,
        messages: list[dict],
        *,
        system: str = "",
        capability: str = "chat",
        prefer: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """token 级流式（无工具场景）：沿降级链找首个可用供应商，逐 token 产出。

        计量在流结束后按累计字符估算（与 complete 的 tokens_out 口径一致）。
        注：结构化输出与 token 级流式互斥（schema 场景走 complete）。
        """
        if prefer is None:
            from ..runtime.canary import current_model_override

            prefer = current_model_override()
        from ..runtime.policy import apply_eval_gate, check_prompt, enforce_chain

        if system:
            messages = [{"role": "system", "content": system}, *messages]
        check_prompt(db, messages)
        chain = enforce_chain(db, self.chain_for(db, capability=capability, prefer=prefer))
        if not chain:
            raise ProviderError(f"没有启用 [{capability}] 能力的模型，请先在模型中心注册")
        # 熔断过滤（M31 任务组 F）：open 的模型从候选链剔除，自然落到降级链下一个
        from ..observability.gateway import get_breaker

        breaker = get_breaker()
        chain = [r for r in chain if breaker.allow(r.name)]
        if not chain:
            raise ProviderError(f"[{capability}] 链上模型均处于熔断状态，请稍后重试")
        # 评测门禁（M42-B）：与 complete 同位同模式，剔除落审计、不抛错
        chain, gate_blocked = apply_eval_gate(db, chain)
        for item in gate_blocked:
            from ..observability import audit

            audit.record("model.eval_gate.blocked", target=item["model"],
                         detail={"reason": item["reason"], "policy": item["policy"],
                                 "capability": capability})
        if not chain:
            raise ProviderError(f"[{capability}] 链上模型均未通过评测门禁，请先在评测中心通过评测")
        errors: list[str] = []
        for record in chain:
            from ..observability.tracing import enabled as otel_enabled, tracer

            span_cm = tracer().start_as_current_span(
                f"llm.stream {record.name}") if otel_enabled() else None
            span = span_cm.__enter__() if span_cm is not None else None
            if span is not None:
                span.set_attribute("llm.model", record.name)
                span.set_attribute("llm.provider", record.provider)
            provider = get_provider(record.provider)
            emitted = 0
            try:
                async for text in provider.stream_complete(
                        record=record, messages=messages, temperature=temperature):
                    emitted += len(text)
                    yield text
                if emitted:
                    from ..observability.middleware import record_usage

                    breaker.record_success(record.name)
                    record_usage("", 0, kind="chat", model=record.name,
                                 tokens_in=sum(max(1, len(str(m.get("content", "")))) // 4 for m in messages),
                                 tokens_out=max(1, emitted // 4), latency_ms=0)
                    if span is not None:
                        span.set_attribute("llm.tokens_out", max(1, emitted // 4))
                    if span_cm is not None:
                        span_cm.__exit__(None, None, None)
                    return
            except ProviderError as e:
                breaker.record_failure(record.name)
                if span is not None:
                    span.record_exception(e)
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)
                if emitted:
                    return  # 已产出部分内容：中断重试会造成重复输出，就此收尾
                errors.append(str(e))
            except Exception:
                breaker.record_failure(record.name)  # 非供应商异常同样计入熔断（防半开探测泄漏）
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)
                raise
        raise ProviderError("所有模型均失败（降级链耗尽）: " + " | ".join(errors))


def _with_schema_instruction(messages: list[dict], schema: dict) -> list[dict]:
    """把 JSON Schema 输出指令追加进 system（无 system 则插入一条）。"""
    note = ("【输出格式】只输出一个符合以下 JSON Schema 的 JSON 对象，"
            "不要包含 markdown 代码块或任何解释文字：\n" + json.dumps(schema, ensure_ascii=False))
    if messages and messages[0].get("role") == "system":
        msgs = [dict(messages[0])]
        msgs[0]["content"] = (msgs[0].get("content") or "") + "\n\n" + note
        return [*msgs, *messages[1:]]
    return [{"role": "system", "content": note}, *messages]


def parse_structured(content: str | None, schema: dict) -> tuple[dict | None, str]:
    """从模型输出提取 JSON 并按 schema 校验 → (data, err)。data=None 时 err 说明原因。"""
    import jsonschema

    if not content:
        return None, "输出为空"
    text = content.strip()
    if "```" in text:  # 剥 markdown 代码围栏
        for part in text.split("```"):
            candidate = part.removeprefix("json").strip()
            if candidate.startswith("{") or candidate.startswith("["):
                text = candidate
                break
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "输出中未找到 JSON 对象"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"
    if not isinstance(data, dict):
        return None, "输出不是 JSON 对象"
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        return None, e.message
    return data, ""


hub = ModelHub()  # 平台单例

"""模型中心路由器：能力路由 + 降级链 + 策略钩子（docs/04 §2.1-2.2 的 M1 版）。

Policy Engine 的 M1 钩子位：route_policies() 可按租户/数据分级扩展过滤（docs/06 §2）。
"""

from __future__ import annotations

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
    ) -> Completion:
        """遍历降级链：供应商失败自动切换下一个（docs/02 §5 ①）。"""
        if prefer is None:
            from ..runtime.canary import current_model_override

            prefer = current_model_override()  # canary 灰度覆盖（显式 prefer 优先）
        from ..runtime.policy import check_prompt, enforce_chain

        check_prompt(db, messages)  # Policy Engine：单次调用 prompt 上限
        chain = enforce_chain(db, self.chain_for(db, capability=capability, prefer=prefer))
        if not chain:
            raise ProviderError(f"没有启用 [{capability}] 能力的模型，请先在模型中心注册")
        errors: list[str] = []
        for record in chain:
            try:
                provider = get_provider(record.provider)
                result = await provider.complete(record=record, messages=messages, tools=tools, temperature=temperature)
                return Completion(result=result, record=record)
            except ProviderError as e:
                errors.append(str(e))
        raise ProviderError("所有模型均失败（降级链耗尽）: " + " | ".join(errors))


hub = ModelHub()  # 平台单例

"""Context Engine 的 M1 版：System 组装 + 上下文预算裁剪（docs/03 §2）。"""

from __future__ import annotations

from ..schemas import Citation


def render_hits(hits: list[dict]) -> str:
    """检索命中 → 编号引用文本（供 Prompt 注入，答案要求标注 [n]）。"""
    if not hits:
        return "（知识库中未检索到相关资料，请如实说明未知。）"
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f"[{i}]（来源：{h['citation']['document']}）{h['content']}")
    return "\n\n".join(lines)


def build_system(*, role: str, knowledge_context: str = "", max_chars: int = 6000) -> str:
    """System Prompt = 角色指令 + 预算内的知识上下文（Context Budget 简化实现）。"""
    parts = [role.strip()]
    if knowledge_context:
        budget = max(0, max_chars - len(parts[0]) - 40)
        ctx = knowledge_context[:budget]
        parts.append(
            "请仅依据以下资料回答，并使用 [n] 标注引用来源；资料中没有的信息请明确说明未知。\n\n"
            + ctx
        )
    return "\n\n".join(parts)


def trim_messages(messages: list[dict], max_chars: int) -> list[dict]:
    """会话超预算：保留 system 与最近消息，压缩最旧的用户/助手消息（字符截断，v0.9.0 行为）。"""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= max_chars:
        return messages
    out = list(messages)
    i = 1  # 跳过 system
    while total > max_chars and i < len(out) - 1:
        c = out[i]
        if c.get("role") in ("user", "assistant") and c.get("content"):
            cut = (c["content"][:200] + "…（已压缩）") if len(c["content"]) > 200 else c["content"]
            total -= len(str(c["content"])) - len(cut)
            c["content"] = cut
        i += 1
    return out


# ---------- LLM 摘要压缩（M34/L7） ----------

_COMPRESS_LOG = None

# 摘要压缩触发下限：待压缩的旧消息少于该条数时不值得一次 LLM 调用，回退字符截断
SUMMARY_MIN_MESSAGES = 2


def _compress_logger():
    global _COMPRESS_LOG
    if _COMPRESS_LOG is None:
        import logging

        _COMPRESS_LOG = logging.getLogger("eap.context")
    return _COMPRESS_LOG


def _summarize_prompt(old: list[dict]) -> list[dict]:
    """构造摘要 prompt：把最旧一组消息压成一段简洁摘要（用户语言中文约定）。"""
    transcript = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in old)
    return [{"role": "user",
             "content": "请把以下历史对话压缩为一段简洁摘要，保留关键事实、结论与未决问题，"
                        "300 字以内，直接输出摘要正文：\n\n" + transcript}]


async def compress_messages(
    db, messages: list[dict], max_chars: int, *,
    session_id: str | None = None, agent: str = "", tenant_id: int | None = None,
    hub=None, keep_recent: int = 2,
) -> list[dict]:
    """会话超预算压缩（M34/L7）：开关开启时用 LLM 摘要替代字符截断。

    行为（EAP_MEMORY_SUMMARY_COMPRESS，默认 off）：
    - off / 总量未超预算：返回原消息（未超预算）或走 trim_messages 字符截断（现行为不变）
    - on 且超预算：把最旧一组消息（保留 system 与最近 keep_recent 条）经 hub.complete
      （capability=chat）生成摘要 → 写入 MemoryRecord(kind=summary, scope=session) 留痕 →
      历史替换为一条摘要条目（role=system）
    - LLM 失败 / 摘要为空 / 待压缩消息不足 SUMMARY_MIN_MESSAGES 条：回退字符截断，不阻断

    各出口均落日志与审计（audit.action = memory.summary_compress / memory.summary_compress_fallback）。
    返回替换后的消息列表（不修改入参）。
    """
    from ..config import get_settings

    messages = [dict(m) for m in messages]  # 副本：不修改入参
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= max_chars:
        return messages
    if not get_settings().memory_summary_compress:
        return trim_messages(messages, max_chars)

    system = [m for m in messages if m.get("role") == "system"][:1]
    rest = [m for m in messages if m.get("role") != "system"]
    if keep_recent < len(rest):
        old, recent = rest[:-keep_recent], rest[-keep_recent:]
    else:
        old, recent = [], rest
    if len(old) < SUMMARY_MIN_MESSAGES:
        return trim_messages(messages, max_chars)

    try:
        if hub is None:
            from ..modelhub.router import hub as default_hub

            hub = default_hub
        completion = await hub.complete(db, _summarize_prompt(old), capability="chat")
        summary = (completion.result.content or "").strip()
        if not summary:
            raise ValueError("LLM 返回空摘要")
    except Exception as e:  # 失败回退字符截断，不阻断会话
        _compress_logger().warning("记忆摘要压缩失败，回退字符截断: %s", e)
        _audit_compress(db, "memory.summary_compress_fallback", session_id, agent,
                        {"error": str(e)[:200], "mode": "truncate"})
        return trim_messages(messages, max_chars)

    # 摘要落库留痕（kind=summary, scope=session，meta 记录压缩来源信息）
    from .memory import memory_service

    record = memory_service.remember(
        db, scope="session", kind="summary", content=summary,
        session_id=session_id, agent=agent, tenant_id=tenant_id,
        meta={"role": "system", "compressed_from": len(old),
              "compressed_chars": sum(len(str(m.get("content") or "")) for m in old),
              "source_roles": [m.get("role", "") for m in old]},
    )
    detail = {"summary_id": record.id, "session_id": session_id, "agent": agent,
              "compressed_messages": len(old), "summary_chars": len(summary)}
    _audit_compress(db, "memory.summary_compress", session_id, agent, detail)
    _compress_logger().info("会话历史已压缩为摘要: %s", detail)

    head = system[0] if system else None
    out = ([head] if head else []) + [
        {"role": "system", "content": f"（历史对话摘要，{len(old)} 条消息压缩）\n{summary}"},
        *recent,
    ]
    # 替换后仍超预算（最近消息本身过长）：对剩余消息补一轮字符截断（摘要条目 role=system 不受影响）
    if sum(len(str(m.get("content") or "")) for m in out) > max_chars:
        out = trim_messages(out, max_chars)
    return out


def _audit_compress(db, action: str, session_id: str | None, agent: str, detail: dict) -> None:
    """压缩动作审计留痕（复用调用方事务，失败仅告警，不阻断）。"""
    try:
        from ..observability import audit

        audit.record(action, actor="system", target=session_id or "-", detail=detail, db=db)
    except Exception as e:  # pragma: no cover - 审计失败不阻断业务
        _compress_logger().warning("摘要压缩审计落库失败: %s", e)


def citations_from(hits: list[dict]) -> list[Citation]:
    return [Citation(**h["citation"]) for h in hits]


# ---------- Prompt 中心渲染（docs/05 §3） ----------

_PROMPT_VAR = None


def render_prompt(template: str, variables: dict[str, str]) -> str:
    """渲染 {{var}} 模板；缺失变量直接报错（发布前应在评测中心验证）。"""
    import re

    global _PROMPT_VAR
    if _PROMPT_VAR is None:
        _PROMPT_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
    missing = sorted({m for m in _PROMPT_VAR.findall(template)} - set(variables))
    if missing:
        raise ValueError(f"Prompt 变量缺失: {missing}")
    return _PROMPT_VAR.sub(lambda m: str(variables[m.group(1)]), template)


def extract_prompt_variables(template: str) -> list[str]:
    import re

    return sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", template)))

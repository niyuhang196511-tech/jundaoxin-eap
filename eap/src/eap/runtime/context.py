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
    """会话超预算：保留 system 与最近消息，压缩最旧的用户/助手消息。"""
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

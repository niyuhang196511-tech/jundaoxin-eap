"""官网客服智能体：绑定 website-faq 知识库，检索增强回答 + 引用溯源。

演示注册钩子 SDK 标准用法：import 即注册 → 平台自动纳管（docs/04 §8）。
支持 token 级流式（on_invoke_stream 重写）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..app import AgentApp
from ..manifest import AgentManifest
from ..sdk import register_agent
from ...runtime.context import build_system
from ...schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="faq-agent",
    version="1.1.0",
    description="官网客服智能体：绑定 website-faq 知识库 + customer-service 技能，回答带 [n] 引用，可发布为嵌入外链",
    models=[{"capability": "chat", "required": True}],
    knowledge=["website-faq"],
    skills=["customer-service"],
    permissions=["kb.retrieve"],
    embeddable=True,
    domains=["*"],
)

SYSTEM_ROLE = "你是企业官网客服助手，礼貌、简洁、准确。"


@register_agent(MANIFEST, source="builtin")
class FaqAgent(AgentApp):
    def _build_context(self, db, request: InvokeRequest):
        """构建对话上下文：检索引用 / Prompt A/B / 技能 / 会话记忆（invoke 与 stream 共用）。"""
        retriever = self.ctx.retriever(MANIFEST.knowledge[0])
        hits = retriever.search(db, request.input, top_k=3)
        skill_ctx = self.ctx.skill_context(MANIFEST.skills)  # 渐进披露 L2：按需加载
        # Prompt 中心 A/B：faq-answer-style 模板存在则按分流键渲染风格指令
        style = ""
        try:
            key = request.session_id or request.user_id
            style = self.ctx.prompt("faq-answer-style", {"question": request.input}, key=key)
        except ValueError:
            pass  # 未配置 Prompt 或变量不匹配 → 回退内置角色
        # 角色提示词：配置版本 system_prompt 覆盖优先（v0.5 Agent 配置版本层），否则内置角色
        role = self.ctx.system_role(SYSTEM_ROLE) + (f"\n\n{style}" if style else "")
        system = build_system(
            role=role + (f"\n\n{skill_ctx}" if skill_ctx else ""),
            knowledge_context=retriever.render(hits),
            max_chars=self.ctx.settings.max_context_chars,
        )
        # 会话记忆：有 session_id 时加载最近对话，实现多轮上下文
        history = (self.ctx.memory.history(db, request.session_id, limit=6)
                   if request.session_id else [])
        if request.user_id:
            recalled = self.ctx.memory.recall(db, request.input, user_id=request.user_id, top_k=2)
            if recalled:
                system += "\n\n【用户长期记忆】\n" + "\n".join(
                    f"- {m['content']}" for m in recalled)
        messages = [*history, {"role": "user", "content": request.input}]
        return hits, messages, system

    def _log_turn(self, db, request: InvokeRequest, answer: str) -> None:
        if request.session_id:
            self.ctx.memory.log_message(db, session_id=request.session_id,
                                        role="user", content=request.input,
                                        agent=MANIFEST.name)
            self.ctx.memory.log_message(db, session_id=request.session_id,
                                        role="assistant", content=answer,
                                        agent=MANIFEST.name)
            db.commit()

    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        with self.ctx.db() as db:
            hits, messages, system = self._build_context(db, request)
            completion = await self.ctx.chat(db, messages=messages, system=system)
            result, record = completion.result, completion.record

            self._log_turn(db, request, result.content or "")

            return InvokeResult(
                content=result.content or "",
                citations=[h["citation"] for h in hits],
                steps=[f"retrieve: {len(hits)} hits from {MANIFEST.knowledge[0]}",
                       f"answer via {record.name}"],
                usage={"tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
                       "model": record.name},
            )

    async def on_invoke_stream(self, request: InvokeRequest) -> AsyncIterator[tuple[str, dict]]:
        """token 级流式：检索 → hub.stream 逐 token 产出 → 会话落库 → result 帧。"""
        with self.ctx.db() as db:
            hits, messages, system = self._build_context(db, request)
            yield "step", {"step": f"retrieve: {len(hits)} hits from {MANIFEST.knowledge[0]}"}
            pieces: list[str] = []
            async for text in self.ctx.hub.stream(db, messages, system=system):
                pieces.append(text)
                yield "token", {"content": text}
            answer = "".join(pieces)
            self._log_turn(db, request, answer)
            yield "result", InvokeResult(
                content=answer,
                citations=[h["citation"] for h in hits],
                steps=[f"retrieve: {len(hits)} hits from {MANIFEST.knowledge[0]}",
                       "answer via chat"],
                usage={"tokens_in": 0, "tokens_out": max(1, len(answer) // 4),
                       "model": "chat"},
            ).model_dump()

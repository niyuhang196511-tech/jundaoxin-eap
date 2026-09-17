"""platform-sdk：手写智能体的平台能力注入（docs/04 §8 / docs/07 §5）。

标准用法：
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import registry, register_agent
    from eap.agents.app import AgentApp

    @register_agent(AgentManifest(name="my-agent", version="1.0.0"))
    class MyAgent(AgentApp):
        async def on_invoke(self, request): ...
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ..db import SessionLocal
from ..modelhub.router import hub as model_hub
from ..schemas import InvokeRequest, InvokeResult
from .app import AgentApp
from .manifest import AgentManifest
from .runtime_tools import retriever_tool

if TYPE_CHECKING:
    from ..runtime.tools import Tool


class Retriever:
    """知识库检索器：search（程序用）/ render（拼 Prompt）/ as_tool（变工具）。"""

    def __init__(self, kb_name: str) -> None:
        self.kb_name = kb_name

    def search(self, db, query: str, top_k: int = 5) -> list[dict]:
        from ..knowledge import service as kb_svc
        from ..models import KB

        kb = db.query(KB).filter_by(name=self.kb_name).first()
        if kb is None:
            return []
        return kb_svc.retrieve(db, kb, query, top_k=top_k)

    @staticmethod
    def render(hits: list[dict]) -> str:
        from ..runtime.context import render_hits

        return render_hits(hits)

    def as_tool(self, top_k: int = 4) -> "Tool":
        return retriever_tool(self.kb_name, top_k)


class PlatformContext:
    """注入给 AgentApp 的平台能力句柄。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.hub = model_hub

    @contextmanager
    def db(self):
        """会话上下文：`with self.ctx.db() as db: ...`"""
        session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def retriever(self, kb_name: str) -> Retriever:
        return Retriever(kb_name)

    async def chat(self, db, messages: list[dict], *, system: str = "", capability: str = "chat",
                   prefer: str | None = None, temperature: float = 0.7):
        """走模型网关的完整对话（路由/降级/计量自动生效）。"""
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        return await self.hub.complete(db, msgs, capability=capability, prefer=prefer, temperature=temperature)

    async def run_loop(self, db, messages: list[dict], *, system: str, tools: list,
                       capability: str = "chat", prefer: str | None = None,
                       approval_gate=None, resume_messages: list[dict] | None = None):
        """完整 Agent Loop（工具调用循环 + HITL 审批门控，docs/03 §1）。"""
        from ..runtime.loop import run_loop

        return await run_loop(
            self.hub, db, messages=messages, system=system, tools=tools,
            capability=capability, prefer=prefer, max_steps=self.settings.agent_max_steps,
            approval_gate=approval_gate, resume_messages=resume_messages,
        )

    def skill_context(self, names: list[str]) -> str:
        """技能渐进披露（L2）：加载指定技能的完整指令文本（docs/04 §3）。"""
        if not names:
            return ""
        from sqlalchemy import select

        from ..models import SkillRecord

        with SessionLocal() as db:
            skills = db.scalars(
                select(SkillRecord).where(SkillRecord.name.in_(names), SkillRecord.enabled == True)  # noqa: E712
            ).all()
            return "\n\n".join(f"【技能：{s.name}】\n{s.instructions}" for s in skills)

    @property
    def memory(self):
        """Memory Service（docs/03 §4）：history / recall / remember / forget。"""
        from ..runtime.memory import memory_service

        return memory_service

    def prompt(self, name: str, variables: dict[str, str], key: str | None = None) -> str:
        """Prompt 中心渲染：模板 + 变量 → 文本（缺失变量报错，docs/05 §3）。

        key（session_id/user_id）提供时参与 A/B 实验分流，返回命中版本的渲染结果。
        """
        from sqlalchemy import select

        from ..models import PromptRecord
        from ..runtime import prompts as prompt_rt
        from ..runtime.context import render_prompt

        with SessionLocal() as db:
            record = db.scalar(
                select(PromptRecord).where(PromptRecord.name == name, PromptRecord.enabled == True)  # noqa: E712
            )
            if record is None:
                raise ValueError(f"Prompt {name} 不存在或未启用")
            template, _version, _exp = prompt_rt.resolve_template(db, name, key)
        return render_prompt(template, variables)

    async def mcp_tools(self, server_url: str, prefix: str = "mcp") -> list:
        """从外部 MCP Server 动态拉取工具（docs/04 §5）。"""
        from ..runtime.mcp_client import load_mcp_tools

        return await load_mcp_tools(server_url, prefix=prefix)

    def connector_tools(self, name: str) -> list:
        """企业连接器工具（docs/04 §4）：按名称取启用连接器的端点工具。"""
        from sqlalchemy import select

        from ..models import ConnectorRecord
        from ..runtime.connectors import load_connector_tools

        with SessionLocal() as db:
            record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
            return load_connector_tools(record) if record else []


def register_agent(manifest: AgentManifest, source: str = "sdk"):
    """注册钩子：装饰 AgentApp 子类，加载期自声明能力（docs/03 §7.2）。

    平台在启动/插件变更时扫描 entry_points(group="eap.agents") 与 EAP_AGENT_MODULES，
    import 即触发本钩子 → 校验 → 版本化写入 Agent Registry。
    """
    from .registry import registry

    def decorator(cls: type[AgentApp]) -> type[AgentApp]:
        cls.manifest = manifest
        registry.register(cls, manifest, source=source, module=cls.__module__)
        return cls

    return decorator


__all__ = [
    "AgentApp", "AgentManifest", "PlatformContext", "Retriever",
    "register_agent", "retriever_tool", "InvokeRequest", "InvokeResult", "json",
]

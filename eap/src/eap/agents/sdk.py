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

        if not self.kb_name:
            return []  # 配置版本白名单拦截：空检索器
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

    @property
    def overlay(self) -> dict:
        """当前调用的配置版本覆盖层（v0.5 Agent 配置版本层；未发布版本为空 dict）。

        由 registry.invoke 解析 published 版本后经 ContextVar 下发，
        agent 代码一般无需直接读取——用 ctx.system_role / chat / run_loop 等缝即可。
        """
        from ..runtime.agent_config import current_overlay

        return current_overlay()

    def system_role(self, default: str) -> str:
        """角色提示词：配置版本的 system_prompt 优先，否则用代码默认。"""
        return self.overlay.get("system_prompt") or default

    def output_schema(self, default: dict | None = None) -> dict | None:
        """生效的结构化输出 schema（v0.5）：请求级 > 配置版本 > 代码默认（default 入参）。"""
        schema = self.overlay.get("output_schema")
        return schema if schema is not None else default

    @staticmethod
    def interact(schema: dict, *, key: str = "input", title: str = "", description: str = "",
                 resume: dict | None = None) -> dict:
        """向用户请求结构化输入（v0.5 交互引擎）。

        用法（on_invoke / on_invoke_task 内）：
            values = ctx.interact(schema, key="warehouse", resume=resume)
        - 首次执行：抛 InteractionRequested —— 任务通道挂起为 WAITING_INPUT（提交后续跑），
          聊天通道返回 InvokeResult.interaction（submit 后以恢复协议重新调用）。
        - 恢复执行：resume 快照带 interactions[key] = 提交值，本函数直接返回提交值，
          agent 代码从交互点继续（无需写两套逻辑）。
        """
        from ..runtime.interaction import InteractionRequested, InteractionRequest

        submitted = (resume or {}).get("interactions", {}).get(key)
        if submitted is not None:
            return submitted
        raise InteractionRequested(
            InteractionRequest(schema=schema, key=key, title=title, description=description),
        )

    @staticmethod
    def interaction_values(request: InvokeRequest) -> dict | None:
        """聊天通道恢复协议：input 为 {"__interaction__": id, ...values} JSON 时返回提交值。"""
        import json as _json

        try:
            parsed = _json.loads(request.input)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict) and "__interaction__" in parsed:
            return {k: v for k, v in parsed.items() if k != "__interaction__"}
        return None

    @property
    def artifacts(self):
        """Artifact Center（v0.5-⑦）：ctx.artifacts.create(name, type, content) → 产物记录。"""
        from ..runtime import artifacts as artifacts_rt

        return _ArtifactsFacade(artifacts_rt)

    def retriever(self, kb_name: str) -> Retriever:
        allowed = self.overlay.get("knowledge")
        if allowed and kb_name not in allowed:
            return Retriever("")  # 配置版本白名单外的知识库不返回内容
        return Retriever(kb_name)

    async def chat(self, db, messages: list[dict], *, system: str = "", capability: str = "chat",
                   prefer: str | None = None, temperature: float | None = None,
                   response_schema: dict | None = None):
        """走模型网关的完整对话（路由/降级/计量自动生效）。

        prefer/temperature 未显式传入时应用配置版本的 model_prefer/temperature 覆盖；
        response_schema（v0.5）触发结构化输出（LLMResult.data 携带校验通过的 JSON）。
        """
        overlay = self.overlay
        if prefer is None:
            prefer = overlay.get("model_prefer") or None
        if temperature is None:
            temperature = overlay.get("temperature", 0.7)
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        return await self.hub.complete(db, msgs, capability=capability, prefer=prefer,
                                       temperature=float(temperature), response_schema=response_schema)

    async def run_loop(self, db, messages: list[dict], *, system: str, tools: list,
                       capability: str = "chat", prefer: str | None = None,
                       approval_gate=None, resume_messages: list[dict] | None = None,
                       max_steps: int | None = None, response_schema: dict | None = None):
        """完整 Agent Loop（工具调用循环 + HITL 审批门控，docs/03 §1）。

        工具清单按配置版本 whitelist 过滤；prefer/max_steps 未显式传入时应用配置版本覆盖；
        response_schema（v0.5）在最终回答后做结构化收尾（RunResult.data）。
        """
        from ..runtime.agent_config import filter_tools
        from ..runtime.loop import run_loop

        overlay = self.overlay
        tools = filter_tools(tools, overlay.get("tools"))
        if prefer is None:
            prefer = overlay.get("model_prefer") or None
        steps = max_steps or overlay.get("max_steps") or self.settings.agent_max_steps
        return await run_loop(
            self.hub, db, messages=messages, system=system, tools=tools,
            capability=capability, prefer=prefer, max_steps=steps,
            approval_gate=approval_gate, resume_messages=resume_messages,
            response_schema=response_schema,
        )

    async def delegated_invoke(self, db, target_agent: str, input: str) -> "InvokeResult":
        """多智能体委派（带 agent-allowlist 策略边界，v0.6-①）。"""
        from ..runtime.policy import check_agent_delegation

        check_agent_delegation(db, target_agent)
        from ..schemas import InvokeRequest

        return await registry.invoke(db, target_agent, InvokeRequest(input=input))

    def skill_context(self, names: list[str]) -> str:
        """技能渐进披露（L2）：加载指定技能的完整指令文本（docs/04 §3）。

        配置版本设置 skills 白名单时，仅加载白名单内的技能。
        """
        allowed = self.overlay.get("skills")
        if allowed:
            names = [n for n in names if n in set(allowed)]
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

    @staticmethod
    def a2a_delegate_tool(transport: object | None = None) -> "Tool":
        """A2A 外部委派工具（M30）：agent 工具清单加入 `a2a.delegate` 后，
        模型可把任务委派给外部 Agent（A2A message/send）。

        边界：外部 endpoint 跨租户 fail-closed，须 a2a-delegate-allowlist 策略放行；
        每次委派落审计 a2a.delegate。transport 仅测试注入用。
        """
        from ..runtime.a2a_client import a2a_delegate_tool

        return a2a_delegate_tool(transport)

    def connector_tools(self, name: str) -> list:
        """企业连接器工具（docs/04 §4）：按名称取启用连接器的端点工具。"""
        from sqlalchemy import select

        from ..models import ConnectorRecord
        from ..runtime.connectors import load_connector_tools

        with SessionLocal() as db:
            record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
            return load_connector_tools(record) if record else []


class _ArtifactsFacade:
    """ctx.artifacts 的轻封装：create 透传 Artifact Center 运行时。"""

    def __init__(self, rt) -> None:
        self._rt = rt

    def create(self, db, *, name: str, type: str, content, mime: str | None = None,
               agent: str | None = None, session_id: str | None = None,
               task_id: str | None = None, ttl_hours: int | None = None,
               trace_id: str = ""):
        return self._rt.create_artifact(
            db, name=name, type=type, content=content, mime=mime,
            agent=agent, session_id=session_id, task_id=task_id,
            ttl_hours=ttl_hours, trace_id=trace_id,
        )


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

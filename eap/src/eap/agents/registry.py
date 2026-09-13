"""Agent Registry：注册钩子 → 自动发现 → 校验 → 版本化纳管（docs/03 §7）。

发现通道（M1）：① entry_points(group="eap.agents")——pip 安装的智能体包
              ② EAP_AGENT_MODULES 指定的模块——import 即触发注册钩子
生命周期（M1）：REGISTER → START → HEALTHY；热加载/灰度/回滚在 M2 制品治理接入。
"""

from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import AgentRecord
from ..schemas import InvokeRequest, InvokeResponse, InvokeResult
from .app import AgentApp
from .manifest import AgentManifest


@dataclass
class RegisteredAgent:
    cls: type[AgentApp]
    manifest: AgentManifest
    source: str
    module: str
    instance: AgentApp | None = None
    status: str = "registered"  # registered | started | stopped | unhealthy
    health: dict = field(default_factory=dict)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    # ---------- 注册（钩子由 sdk.register_agent 调用） ----------

    def register(self, cls: type[AgentApp], manifest: AgentManifest, source: str = "sdk", module: str = "") -> None:
        if not (isinstance(cls, type) and issubclass(cls, AgentApp)):
            raise TypeError("register_agent 只接受 AgentApp 子类")
        existing = self._agents.get(manifest.name)
        if existing is not None:
            if existing.manifest.version == manifest.version and existing.cls is cls:
                return  # 重复 import 幂等
            if existing.module and existing.module == module:
                self._replace(existing, cls, manifest, source)  # 热加载：同模块重注册 = 替换
                return
            raise ValueError(f"智能体 {manifest.name} 已注册（v{existing.manifest.version}），"
                             f"请升级版本号或更名：冲突 v{manifest.version} from {module}")
        self._agents[manifest.name] = RegisteredAgent(
            cls=cls, manifest=manifest, source=source, module=module,
            status="registered", health={},
        )

    def _replace(self, existing: RegisteredAgent, cls: type[AgentApp],
                 manifest: AgentManifest, source: str) -> None:
        """热加载替换：换上新类，状态回到待启动（调用方须已 stop，见 reload_modules）。"""
        self._agents[manifest.name] = RegisteredAgent(
            cls=cls, manifest=manifest, source=source, module=existing.module,
            status="registered", health={},
        )

    def get(self, name: str) -> RegisteredAgent:
        agent = self._agents.get(name)
        if agent is None:
            raise KeyError(f"智能体 {name} 未注册")
        return agent

    def names(self) -> list[str]:
        return sorted(self._agents)

    def all(self) -> list[RegisteredAgent]:
        return [self._agents[n] for n in sorted(self._agents)]

    # ---------- 生命周期 ----------

    async def start_agent(self, name: str) -> None:
        """实例化 + START + 健康检查 + DB 纳管（bootstrap 与动态注册共用）。"""
        agent = self._agents[name]
        ctx = get_platform_context()
        try:
            agent.instance = agent.cls(ctx)
            await agent.instance.on_register()
            await agent.instance.on_start()
            agent.health = await agent.instance.health_check() or {}
            agent.status = "started" if agent.health.get("ok", True) else "unhealthy"
        except Exception as e:
            agent.status = "unhealthy"
            agent.health = {"ok": False, "error": str(e)}
        self._persist(agent)

    async def stop_agent(self, name: str) -> None:
        """STOP：调用 on_stop 钩子，实例保留（start 可再拉起），拒绝后续调用。"""
        agent = self._agents[name]
        if agent.instance is not None and agent.status == "started":
            try:
                await agent.instance.on_stop()
            except Exception as e:
                agent.health = {"ok": False, "stop_error": str(e)}
        agent.status = "stopped"
        agent.health = {"ok": False, "reason": "stopped by operator"}
        self._persist(agent)

    async def unregister(self, name: str) -> None:
        """注销：停实例 + 移出注册表 + 删除 DB 纳管记录。"""
        if self._agents[name].status == "started":
            await self.stop_agent(name)
        del self._agents[name]
        with SessionLocal() as db:
            record = db.scalar(select(AgentRecord).where(AgentRecord.name == name))
            if record is not None:
                db.delete(record)
                db.commit()

    async def reload_modules(self) -> int:
        """热加载：先停全部运行中实例（触发 on_stop 钩子），importlib.reload 配置模块
        （同模块重注册 = 替换），再统一重新启动。返回重载的模块数。"""
        settings = get_settings()
        builtin = ["eap.agents.builtin.faq_agent",
                   "eap.agents.builtin.order_agent",
                   "eap.agents.builtin.supervisor_agent"]
        modules = list(dict.fromkeys([*builtin, *settings.agent_modules]))
        for name in self._agents:
            if self._agents[name].status == "started":
                await self.stop_agent(name)
        reloaded = 0
        for mod in modules:
            try:
                if mod in importlib.sys.modules:
                    importlib.reload(importlib.sys.modules[mod])
                else:
                    importlib.import_module(mod)
                reloaded += 1
            except Exception as e:
                print(f"[registry] 热加载模块 {mod} 失败: {e}")
        for name in self._agents:
            await self.start_agent(name)
        return reloaded

    async def bootstrap(self) -> None:
        """启动引导：发现 → import（触发注册钩子）→ 逐个启动。"""
        settings = get_settings()

        # ① entry_points 发现（pip 安装的智能体包）
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group="eap.agents"):
                try:
                    ep.load()
                except Exception as e:  # 单个包失败不影响平台
                    print(f"[registry] entry_point {ep.name} 加载失败: {e}")
        except Exception:
            pass

        # ② 配置模块发现（内置示例 + EAP_AGENT_MODULES；env 覆盖不挤掉内置）
        builtin = ["eap.agents.builtin.faq_agent",
                   "eap.agents.builtin.order_agent",
                   "eap.agents.builtin.supervisor_agent"]
        modules = dict.fromkeys([*builtin, *settings.agent_modules])
        for mod in modules:
            try:
                importlib.import_module(mod)
            except Exception as e:
                print(f"[registry] 模块 {mod} 加载失败: {e}")

        # ③ 逐个启动
        for name in self._agents:
            await self.start_agent(name)

    def _persist(self, agent: RegisteredAgent) -> None:
        with SessionLocal() as db:
            record = db.scalar(select(AgentRecord).where(AgentRecord.name == agent.manifest.name))
            if record is None:
                record = AgentRecord(name=agent.manifest.name)
                db.add(record)
            record.version = agent.manifest.version
            record.description = agent.manifest.description
            record.manifest = agent.manifest.model_dump()
            record.source = agent.source
            record.module = agent.module
            record.status = agent.status
            record.health = agent.health
            db.commit()

    # ---------- 调用 ----------

    async def invoke(self, db: Session, name: str, request: InvokeRequest, trace_id: str | None = None) -> InvokeResponse:
        agent = self.get(name)
        if agent.status == "unhealthy":
            raise RuntimeError(f"智能体 {name} 健康检查未通过：{agent.health}")
        if agent.status == "stopped":
            raise RuntimeError(f"智能体 {name} 已停用（stop），请先 start")
        assert agent.instance is not None
        # Canary 灰度（docs/06 §2）：命中则注入 overrides.model，响应标记 canary
        canary_info = None
        token = None
        try:
            from ..runtime.canary import apply_override, pick_canary, reset_override

            key = request.user_id or request.session_id or (trace_id or "")
            release = pick_canary(db, name, key)
            if release is not None:
                token = apply_override((release.overrides or {}).get("model"))
                canary_info = {"release_id": release.id, "version": release.version,
                               "percent": release.canary_percent}
        except Exception:
            pass  # 灰度失败不阻断主流程
        try:
            result: InvokeResult = await agent.instance.on_invoke(request)
        finally:
            if token is not None:
                from ..runtime.canary import reset_override

                reset_override(token)
        return InvokeResponse(
            invocation_id=uuid.uuid4().hex,
            agent=agent.manifest.name,
            agent_version=agent.manifest.version,
            trace_id=trace_id or uuid.uuid4().hex,
            output=result.content,
            citations=result.citations,
            steps=result.steps,
            usage=result.usage,
            canary=canary_info,
        )


_ctx_singleton = None


def get_platform_context():
    """懒加载单例 PlatformContext（避免 import 环）。"""
    global _ctx_singleton
    if _ctx_singleton is None:
        from .sdk import PlatformContext

        _ctx_singleton = PlatformContext(get_settings())
    return _ctx_singleton


registry = AgentRegistry()

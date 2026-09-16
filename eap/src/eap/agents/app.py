"""AgentApp 基类：平台智能体的生命周期契约（docs/03 §7.1）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from ..schemas import InvokeRequest, InvokeResult
from .manifest import AgentManifest


class AgentApp:
    """手写智能体继承本类并经 @register_agent 注册。

    生命周期：INSTALL → DISCOVER → VALIDATE → REGISTER → LOAD → START ⇄ HEALTHY
              → STOP → UNLOAD → UPGRADE/ROLLBACK
    M1 实现 START/HEALTHY/STOP/INVOKE 钩子；install/uninstall 与热加载在制品治理（M2）接入。
    """

    manifest: ClassVar[AgentManifest | None] = None

    def __init__(self, ctx) -> None:
        self.ctx = ctx  # PlatformContext（平台能力注入：模型网关/知识检索/会话）

    async def on_register(self) -> None:
        """注册后回调：声明生效确认。"""

    async def on_start(self) -> None:
        """LOAD→START：构建运行时对象（LLM/检索器/工具）。"""

    async def health_check(self) -> dict:
        """健康探针：Registry 据此上架/摘流。"""
        return {"ok": True}

    async def on_stop(self) -> None:
        """优雅停止：排空在途请求。"""

    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        raise NotImplementedError(f"{type(self).__name__} 未实现 on_invoke")

    async def on_invoke_stream(self, request: InvokeRequest) -> AsyncIterator[tuple[str, dict]]:
        """流式调用（SSE token 打字机）：产出 (event, data) 序列。

        event: "token" {"content": str} | "result" InvokeResult.dump。
        默认实现回退为整段调用：先 result 前产出一个 token 帧（前端协议统一）。
        需要 token 级流式的智能体应重写本方法（参考 FaqAgent）。
        """
        result = await self.on_invoke(request)
        content = result.content or ""
        if content:
            yield "token", {"content": content}
        yield "result", result.model_dump()

    async def on_invoke_task(self, request: InvokeRequest, gate=None, resume: dict | None = None) -> InvokeResult:
        """任务引擎执行入口（Task/Job + HITL）。

        gate(tool_name) -> True 执行 / False 否决 / None 挂起等待人工；
        resume 为挂起快照（messages + approvals），续跑时由引擎回传。
        默认忽略审批语义，需要 HITL 的智能体应重写本方法。
        """
        return await self.on_invoke(request)

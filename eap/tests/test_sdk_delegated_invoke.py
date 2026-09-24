"""PlatformContext.delegated_invoke 契约测试（M51-A）：SDK 多智能体委派缝——
policy 边界校验后经 registry.invoke 调用下级，InvokeResponse 收敛为 InvokeResult
（修复前该缝引用未定义名 registry，调用即 NameError，且无任何覆盖）。"""

from __future__ import annotations


def test_delegated_invoke_returns_invoke_result(client):
    import asyncio

    from eap.agents.app import AgentApp
    from eap.agents.manifest import AgentManifest
    from eap.agents.registry import get_platform_context, registry
    from eap.db import SessionLocal
    from eap.schemas import InvokeRequest, InvokeResult

    manifest = AgentManifest(name="m51a-echo", version="1.0.0")

    class EchoAgent(AgentApp):
        async def on_invoke(self, request: InvokeRequest):
            return InvokeResult(content=f"echo:{request.input}", steps=["s1"],
                                data={"k": 1}, data_schema={"type": "object"})

    registry.register(EchoAgent, manifest)
    agent = registry.get("m51a-echo")
    agent.instance = EchoAgent(get_platform_context())

    ctx = get_platform_context()
    with SessionLocal() as db:
        result = asyncio.run(ctx.delegated_invoke(db, "m51a-echo", "hi"))
    assert isinstance(result, InvokeResult)
    assert result.content == "echo:hi"
    assert result.steps == ["s1"]
    assert result.data == {"k": 1}

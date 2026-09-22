"""EAP 平台入口：装配中间件、路由、生命周期（初始化/种子/Agent 注册引导）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agents.registry import registry
from .api.v1 import a2a as api_a2a
from .api.v1 import actions as api_actions
from .api.v1 import agents as api_agents
from .api.v1 import artifacts as api_artifacts
from .api.v1 import budgets as api_budgets
from .api.v1 import audit as api_audit
from .api.v1 import auth as api_auth
from .api.v1 import chat as api_chat
from .api.v1 import connectors as api_connectors
from .api.v1 import conversations as api_conversations
from .api.v1 import embed as api_embed
from .api.v1 import im as api_im
from .api.v1 import evals as api_evals
from .api.v1 import extensions as api_extensions
from .api.v1 import kb as api_kb
from .api.v1 import lora as api_lora
from .api.v1 import memory as api_memory
from .api.v1 import mcp_registry as api_mcp_registry
from .api.v1 import models as api_models
from .api.v1 import prompts as api_prompts
from .api.v1 import policies as api_policies
from .api.v1 import releases as api_releases
from .api.v1 import skills as api_skills
from .api.v1 import tasks as api_tasks
from .api.v1 import triggers as api_triggers
from .api.v1 import webhooks as api_webhooks
from .api.v1 import workflows as api_workflows
from .db import init_db
from .config import get_settings
from .mcp_server import create_mcp_server
from .observability.middleware import TraceMiddleware
from .runtime.events import bus
from .runtime.tasks import create_task_engine
from .runtime.triggers import TriggerEngine
from .runtime.webhooks import WebhookEngine


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .observability import tracing

    tracing.setup()  # OTel（EAP_OTEL_ENDPOINT 配置时启用）
    init_db()
    await registry.bootstrap()
    from . import workflows as workflows_svc

    await workflows_svc.load_enabled()
    from .agents import registry_sync
    from .plugins import load_plugins

    load_plugins()  # 扩展开发体系：插件目录加载（单插件失败不阻断启动）
    registry_sync.start_subscriber(registry.apply_remote_event)  # M10：跨副本管理操作广播
    # M32：worker 数取 EAP_WORKER_COUNT——HA 部署下 API 实例设 0（执行交给独立 worker 进程，deploy/compose）
    await app.state.task_engine.start(workers=get_settings().worker_count)
    # 事件中心（M30）：先起事件总线，触发引擎再订阅（规则 CRUD 后经 reload 即时生效）
    app.state.trigger_engine = TriggerEngine(app.state.task_engine)
    await bus.start()
    await app.state.trigger_engine.start()
    # 对外 Webhook 推送（M31）：订阅总线事件 → HMAC 签名推送（端点 CRUD 后经 reload 即时生效）
    app.state.webhook_engine = WebhookEngine()
    await app.state.webhook_engine.start()
    async with app.state.mcp.session_manager.run():  # MCP Streamable HTTP 会话管理
        yield
    await app.state.webhook_engine.stop()
    await app.state.trigger_engine.stop()
    await bus.stop()
    await registry_sync.stop_subscriber()
    await app.state.task_engine.stop()
    for agent in registry.all():
        if agent.instance:
            try:
                await agent.instance.on_stop()
            except Exception:
                pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="EAP · 企业级 Agent 智能体平台",
        version=__version__,
        description="三面七层架构 M1 核心：模型中心 / 知识中心 / Agent Runtime / 注册钩子 SDK",
        lifespan=lifespan,
    )
    app.add_middleware(TraceMiddleware)
    origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
    if origins:  # 默认空 = 仅同源（不挂 CORS）；跨域部署显式配置 EAP_CORS_ORIGINS
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
        )
    # API Gateway（M31 任务组 F）：add_middleware 为 LIFO（后注册者在外层），
    # 故逆序注册 → 请求流经顺序 = RequestSize(413) → Concurrency(429/超时504) → Idempotency(重放)
    from .observability.gateway import ConcurrencyLimitMiddleware, IdempotencyMiddleware, RequestSizeLimitMiddleware

    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.include_router(api_chat.router)
    app.include_router(api_connectors.router)
    app.include_router(api_agents.router)
    app.include_router(api_budgets.router)
    app.include_router(api_actions.router)
    app.include_router(api_artifacts.router)
    app.include_router(api_auth.router)
    app.include_router(api_audit.router)
    # 知识文档图片（M16）：/media 静态服务（media_dir 按内容哈希去重存储）

    media_dir = get_settings().media_dir
    os.makedirs(media_dir, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_dir), name="media")
    app.include_router(api_a2a.router)
    app.include_router(api_a2a.wellknown)
    app.include_router(api_kb.router)
    app.include_router(api_memory.router)
    app.include_router(api_conversations.router)
    app.include_router(api_extensions.router)
    app.include_router(api_mcp_registry.router)
    app.include_router(api_models.router)
    app.include_router(api_lora.router)  # LoRA adapter 托管（M42-A）
    app.include_router(api_embed.router)
    app.include_router(api_im.router)
    app.include_router(api_tasks.router)
    app.include_router(api_triggers.router)
    app.include_router(api_triggers.public_router)  # 入站 webhook（公开端点，签名即凭证）
    app.include_router(api_webhooks.router)  # 对外 Webhook 推送（M31：端点/投递/重投/试投）
    app.include_router(api_skills.router)
    app.include_router(api_workflows.router)
    app.include_router(api_prompts.router)
    app.include_router(api_policies.router)
    app.include_router(api_releases.router)
    app.include_router(api_evals.router)
    app.include_router(api_embed.public_router)

    # 嵌入外链静态资源：/sdk/eap-widget.js、/demo.html（第三方站点嵌入 widget）；
    # 控制台前端已独立部署（frontend/ → Next.js），不再由后端托管
    static_dir = Path(__file__).parent / "static"
    app.mount("/sdk", StaticFiles(directory=static_dir), name="sdk")

    # MCP Server：平台能力以标准 MCP 工具暴露（Claude/Cursor/Harness 直连，需 API Key）
    mcp, mcp_app = create_mcp_server()
    app.state.mcp = mcp
    app.router.routes.extend(mcp_app.routes)
    if get_settings().mcp_auth:
        from .mcp_server import MCPAuthMiddleware

        app.add_middleware(MCPAuthMiddleware)
    # Task/Job 引擎（每实例独立）
    app.state.task_engine = create_task_engine()

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "agents": {a.manifest.name: a.status for a in registry.all()},
        }

    @app.get("/metrics")
    def metrics():
        """Prometheus text exposition（进程内计数器，M7）。"""
        from fastapi.responses import PlainTextResponse

        from .observability.metrics import render

        return PlainTextResponse(render(), media_type="text/plain; version=0.0.4")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    from .config import get_settings

    s = get_settings()
    uvicorn.run("eap.main:app", host=s.host, port=s.port)


if __name__ == "__main__":
    main()

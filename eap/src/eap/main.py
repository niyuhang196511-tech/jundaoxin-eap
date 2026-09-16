"""EAP 平台入口：装配中间件、路由、生命周期（初始化/种子/Agent 注册引导）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agents.registry import registry
from .api.v1 import a2a as api_a2a
from .api.v1 import agents as api_agents
from .api.v1 import budgets as api_budgets
from .api.v1 import auth as api_auth
from .api.v1 import chat as api_chat
from .api.v1 import connectors as api_connectors
from .api.v1 import embed as api_embed
from .api.v1 import im as api_im
from .api.v1 import evals as api_evals
from .api.v1 import kb as api_kb
from .api.v1 import memory as api_memory
from .api.v1 import mcp_registry as api_mcp_registry
from .api.v1 import models as api_models
from .api.v1 import prompts as api_prompts
from .api.v1 import policies as api_policies
from .api.v1 import releases as api_releases
from .api.v1 import skills as api_skills
from .api.v1 import tasks as api_tasks
from .api.v1 import workflows as api_workflows
from .db import init_db
from .config import get_settings
from .mcp_server import create_mcp_server
from .observability.middleware import TraceMiddleware
from .runtime.tasks import create_task_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await registry.bootstrap()
    from . import workflows as workflows_svc

    await workflows_svc.load_enabled()
    await app.state.task_engine.start(workers=2)
    async with app.state.mcp.session_manager.run():  # MCP Streamable HTTP 会话管理
        yield
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
    origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(api_chat.router)
    app.include_router(api_connectors.router)
    app.include_router(api_agents.router)
    app.include_router(api_budgets.router)
    app.include_router(api_auth.router)
    app.include_router(api_a2a.router)
    app.include_router(api_a2a.wellknown)
    app.include_router(api_kb.router)
    app.include_router(api_memory.router)
    app.include_router(api_mcp_registry.router)
    app.include_router(api_models.router)
    app.include_router(api_embed.router)
    app.include_router(api_im.router)
    app.include_router(api_tasks.router)
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

    return app


app = create_app()


def main() -> None:
    import uvicorn

    from .config import get_settings

    s = get_settings()
    uvicorn.run("eap.main:app", host=s.host, port=s.port)


if __name__ == "__main__":
    main()

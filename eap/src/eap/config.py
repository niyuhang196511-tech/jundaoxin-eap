"""平台配置：一切可经 EAP_ 前缀环境变量覆盖。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EAP_", env_file=".env", extra="ignore")

    # 服务
    host: str = "192.168.0.7"
    port: int = 8300
    db_url: str = "sqlite:///./eap.db"

    # CORS 白名单（逗号分隔 origin）：控制台前端独立部署时填其来源；"*" 全放行（仅开发）
    cors_origins: str = "*"

    # 开发租户与密钥（生产走 OIDC/SSO，见 docs/06）
    dev_api_key: str = "dev-key-1"
    dev_tenant: str = "dev"

    # 模型中心：外部 OpenAI 兼容供应商（不配置则仅用内置 mock，平台可完全离线运行）
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # 知识中心：嵌入提供方 hash=离线确定性嵌入（开发用）；可切 openai
    embedding_provider: str = "hash"
    embed_dim: int = 256

    # Runtime：上下文预算（字符级，M1 简化实现）
    max_context_chars: int = 6000
    agent_max_steps: int = 4

    # 注册钩子：除 entry_points 外额外扫描的模块（示例智能体等）
    agent_modules: list[str] = ["eap.agents.builtin.faq_agent"]

    # 嵌入外链：会话令牌签名密钥与时效（docs/04 §6）
    session_secret: str = "dev-session-secret-change-me"
    embed_session_ttl: int = 2 * 3600  # 秒
    embed_rate_limit: int = 60  # 每分钟每令牌请求数

    # 技能包签名密钥（32 字节 hex，Ed25519 seed）：技能市场分发链的信任根（docs/04 §3）
    skill_signing_key: str | None = None  # 未配置用开发默认密钥，生产必换

    # OIDC/SSO（docs/06 §1）：配置 issuer+client_id 即启用；tenant 映射见 docs/05 §5
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str = "http://localhost:8300/api/v1/auth/oidc/callback"

    # 多副本任务队列（docs/03 §5）：配置 Redis 后任务经 Streams 跨实例分发
    redis_url: str | None = None

    # 向量路生产形态（docs/04 §1）：配置 Milvus 后向量检索走 Milvus（不可达自动回退本地余弦）
    milvus_uri: str | None = None

    # 图谱实体抽取（docs/04 §1）：lexical=词元共现（离线）；llm=模型结构化抽取（失败回退共现）
    graph_extraction: str = "lexical"

    # MCP 端点鉴权（docs/04 §5）：默认开启（平台 API Key）；内网可信环境可关闭
    mcp_auth: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()

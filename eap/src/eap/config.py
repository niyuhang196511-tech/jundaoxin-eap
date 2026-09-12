"""平台配置：一切可经 EAP_ 前缀环境变量覆盖。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EAP_", env_file=".env", extra="ignore")

    # 服务
    host: str = "0.0.0.0"
    port: int = 8300
    db_url: str = "sqlite:///./eap.db"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

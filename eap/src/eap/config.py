"""平台配置：一切可经 EAP_ 前缀环境变量覆盖。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EAP_", env_file=".env", extra="ignore")

    # 服务：默认仅绑定回环（生产经反代暴露；多网卡用 EAP_HOST 显式指定）
    host: str = "127.0.0.1"
    port: int = 8300
    db_url: str = "sqlite:///./eap.db"

    # M47-C 启动 fail-fast：置 1 时存在开发默认密钥 / 必配密钥缺失即拒绝启动
    # （deploy/docker-compose.prod.yml 默认开启；开发保持 0 走警告不阻断）
    strict_config: bool = False

    # CORS 白名单（逗号分隔 origin）：默认空 = 仅同源；"*" 全放行（仅开发）
    cors_origins: str = ""

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
    # 资源服务器模式（M6）：外部 IdP access token 的 aud 约束（空=不校验）
    jwt_audience: str | None = None

    # 静态秘密加密密钥（M8）：模型/连接器的 api_key 静态存储加密（Fernet）；未配置 = 明文存储（仅开发）
    secret_key: str | None = None
    # 密钥轮换（M48-C）：旧密钥列表（逗号分隔）——EAP_SECRET_KEY 换新后把旧值放这里，
    # 存量密文解密自动回退；存量重加密跑 scripts/reencrypt_secrets.py，完成后移除本变量
    secret_key_previous: str | None = None

    # OTel tracing（M9）：配置 OTLP 端点即启用（如 http://otel-collector:4317）；未配置零开销
    otel_endpoint: str | None = None
    otel_sample_ratio: float = 1.0

    # 文档解析（M12/M14）：默认后端 local；MinerU 支持 PDF 扫描件/复杂版式 → Markdown
    docs_parser: str = "local"
    mineru_api_url: str = "https://mineru.net/api/v4"  # 自托管填 http://host:port
    mineru_token: str | None = None

    # 文档图片（M16）：内嵌图/MinerU 图落盘到 EAP_MEDIA_DIR 并挂 /media 静态服务；
    # 配置 EAP_VISION_MODEL（OpenAI 兼容视觉模型，走 EAP_OPENAI_BASE_URL）后图片自动生成中文描述进 chunk
    media_dir: str = "./media"
    docs_extract_images: bool = True
    vision_model: str | None = None

    # 多副本任务队列（docs/03 §5）：配置 Redis 后任务经 Streams 跨实例分发
    redis_url: str | None = None

    # 向量路生产形态（docs/04 §1）：配置 Milvus 后向量检索走 Milvus（不可达自动回退本地余弦）
    milvus_uri: str | None = None

    # 图谱实体抽取（docs/04 §1）：lexical=词元共现（离线）；llm=模型结构化抽取（失败回退共现）
    graph_extraction: str = "lexical"
    judge_model: str = ""  # LLM-as-Judge 使用的模型（空 = 按 chat 能力路由）
    chat_rate_limit: int = 120  # 每分钟对话调用上限（/v1/chat 与 agents invocations，按凭证）
    memory_retention_days: int = 180  # 记忆保留期（天），超期可由 /memory/purge 清理
    # 记忆摘要压缩（M34/L7）：开启后会话消息超预算时经 LLM 生成摘要替换旧消息（默认 off 保持字符截断）
    memory_summary_compress: bool = False

    # 审计日志治理（M47-B）：导出行数上限必须存在（防拖库）——0/负值视为非法配置，回落默认
    audit_export_limit: int = 50000
    # 审计保留期（天）：超过保留期的审计行由 POST /audit/purge 清理；0=永久保留（禁用清理）
    audit_retention_days: int = 365

    # MCP 端点鉴权（docs/04 §5）：默认开启（平台 API Key）；内网可信环境可关闭
    mcp_auth: bool = True

    # 测试子进程跳过 Alembic（库已 head；SQLite 父子进程并发 upgrade 会锁等待）
    skip_migrations: bool = False

    # 插件目录（扩展开发体系）：手写工具/RAG 组件/Agent 的目录发现点，启动与 /plugins/reload 时加载
    plugins_dir: str = "./plugins"

    # IM 投递重试队列（M30 任务组 D）：进程内 asyncio 指数退避（无 Redis 依赖）。
    # 退避间隔 = base * 2^(attempts-1)，封顶 max_seconds；poll_seconds 为空闲扫描周期
    im_retry_max_attempts: int = 5
    im_retry_base_seconds: float = 2.0
    im_retry_max_seconds: float = 300.0
    im_retry_poll_seconds: float = 5.0

    # 对外 Webhook 推送（M31 任务组 B）：进程内 asyncio 指数退避（无 Redis 依赖）。
    # 退避间隔 = base * 2^(attempts-1)，封顶 max_seconds；timeout_s 为单次 HTTP POST 超时
    webhook_max_attempts: int = 5
    webhook_base_seconds: float = 2.0
    webhook_max_seconds: float = 300.0
    webhook_timeout_s: float = 10.0

    # API Gateway（M31 任务组 F）：全局防护默认关闭（0），熔断/幂等按需激活；
    # 幂等无需总开关——仅带 Idempotency-Key 头的写请求才进入幂等通道
    gateway_max_concurrency: int = 0  # 每凭证在途请求上限（0=关闭）
    gateway_max_body_bytes: int = 10 * 1024 * 1024  # 请求体上限字节（0=关闭；默认 10MB）
    gateway_timeout_s: float = 0.0  # 非流式请求超时秒（0=关闭；流式路径始终豁免）
    gateway_cb_window_s: float = 60.0  # 熔断滑动窗口（秒）
    gateway_cb_rate: float = 0.5  # 熔断失败率阈值（窗口内失败占比 ≥ 该值跳闸）
    gateway_cb_min_samples: int = 5  # 熔断最小样本数（窗口样本不足不跳闸）
    gateway_cb_cooldown_s: float = 30.0  # open → half-open 冷却（秒）
    gateway_idempotency_ttl_s: int = 300  # 幂等键结果保留时长（秒）

    # SQL 连接器（M31 任务组 C）：单次只读查询返回行数上限（超出截断并标记 truncated=true）
    connector_sql_max_rows: int = 200

    # 独立 Worker 进程（M32 任务组 P1）：python -m eap.worker
    worker_count: int = 2  # worker 并发协程数（单进程内取任务执行的并发度）
    worker_lease_seconds: int = 300  # 任务执行租约（秒）：到期未完成视为 worker 崩溃，重置 PENDING 重跑


@lru_cache
def get_settings() -> Settings:
    return Settings()

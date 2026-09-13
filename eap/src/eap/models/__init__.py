"""ORM 领域模型 —— docs/05 领域模型的 M1 子集。

生产环境需加 tenant_id 列 + PostgreSQL RLS（docs/05 §5），此处为开发版。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserRecord(Base):
    """平台用户（docs/06 §1，M3 OIDC/SSO）：OIDC sub 唯一定位，登录换发租户 API Key。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(256), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    """凭证体系 M1 子集：服务间 API Key（用户 Token/EmbedToken 见 docs/07 §1）。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    note: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelRecord(Base):
    """模型中心注册表：LLM 与专用模型同表，按能力（capability）路由（docs/04 §2）。"""

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="mock")  # mock | openai_compat
    base_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    remote_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # 小者优先
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRecord(Base):
    """Agent Registry：注册钩子纳管的智能体（生命周期状态见 docs/03 §7）。"""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(16), default="sdk")  # builtin | sdk | entrypoint
    module: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="registered")  # registered|started|unhealthy
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KB(Base):
    """知识库实例（多 KB 模型，docs/04 §1）。"""

    __tablename__ = "kbs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    template: Mapped[str] = mapped_column(String(16), default="doc")  # doc | faq
    embedding_provider: Mapped[str] = mapped_column(String(16), default="hash")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kbs.id"), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(String(256), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("kbs.id"), index=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class TaskRecord(Base):
    """Task/Job 8 态状态机的 M1 子集（docs/03 §5）。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), default="generic")
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UsageRecord(Base):
    """成本中心计量事实表（docs/08 §4）。"""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat | agent | embed
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BudgetRecord(Base):
    """成本中心·租户预算（docs/08 §4）：按自然月统计 token 用量，超限熔断调用。"""

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    monthly_token_budget: Mapped[int] = mapped_column(Integer, default=0)  # 0 = 不限
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PolicyRecord(Base):
    """Policy Engine（docs/02 ①，M3）：模型中心租户策略，模型网关路由时强制执行。

    - tenant_id=0 为平台默认策略，具体租户策略按 priority 升序先于默认生效
    - kind=model-allowlist：config={"models": [...]} 只允许路由到指定模型
    - kind=provider-allowlist：config={"providers": ["mock", ...]} 数据不出域（只允许本地/内网供应商）
    - kind=max-prompt-tokens：config={"limit": N} 单次调用 prompt token 预算上限
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0 = 平台默认
    kind: Mapped[str] = mapped_column(String(24), default="model-allowlist")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # 小者先生效
    notes: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IMChannelRecord(Base):
    """企业 IM 渠道（docs/04 §4，M3）：飞书/钉钉/企业微信 机器人接入。

    - 推送：群机器人 Webhook（三平台格式不同，runtime/im.py 统一封装）
    - 接入：回调端点 /api/v1/im/{platform}/{name}/webhook → 路由到绑定智能体，回复推回群
    - secret：飞书=Verification Token；钉钉=加签密钥；企业微信用 extra={"token","aes_key"}
    """

    __tablename__ = "im_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(16))  # feishu | dingtalk | wecom
    agent: Mapped[str] = mapped_column(String(64))  # 接入消息路由到的智能体
    webhook_url: Mapped[str] = mapped_column(String(512), default="")  # 群机器人推送地址
    secret: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # 企业微信: {"token","aes_key"}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConnectorRecord(Base):
    """企业连接器（docs/04 §4，M3）：把外部系统的端点注册为平台工具。

    - kind=rest：base_url + endpoint path 经 httpx 调用（管理端登记，超时熔断）
    - kind=mock-erp：内置离线演示 ERP（库存查询/下单），测试与开发用
    - endpoints：[{tool_name, method, path, description, params, requires_approval}]
    """

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="rest")  # rest | mock-erp
    description: Mapped[str] = mapped_column(String(256), default="")
    base_url: Mapped[str] = mapped_column(String(256), default="")
    header_name: Mapped[str] = mapped_column(String(64), default="Authorization")
    api_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    endpoints: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="registered")  # registered | verified | unreachable
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SkillRecord(Base):
    """技能注册表（docs/04 §3）：SKILL.md 的结构化存储。

    渐进披露：L1 = name+description（目录，零成本）；L2 = instructions 全文（按需注入）。
    scripts/references/assets 与签名打包在 M2 技能市场接入。
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(String(256), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowRecord(Base):
    """工作流 DSL 存储（docs/03 §6 Workflow Engine）：启停 + 版本，启动时注册为智能体。"""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    dsl: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskScheduleRecord(Base):
    """定时调度（docs/03 §5，M3）：按固定间隔周期性提交任务（DB 持久化，重启不丢）。

    MVP 用固定间隔（interval_seconds）；cron 表达式接入时复用同一执行链路。
    """

    __tablename__ = "task_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    task_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GraphNodeRecord(Base):
    """知识图谱节点（docs/04 §1 三路索引之图谱路，M3 离线 MVP）：

    实体 = 词元级（Han 双字组 / ASCII 词，去停用词）；真实实体抽取接 LLM 后替换。
    """

    __tablename__ = "graph_nodes"
    __table_args__ = (UniqueConstraint("kb_id", "entity", name="uq_gnode_kb_entity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True)
    entity: Mapped[str] = mapped_column(String(64), index=True)


class GraphEdgeRecord(Base):
    """知识图谱边：实体在 chunk 内共现（weight=共现次数），召回时按边找回 chunk。"""

    __tablename__ = "graph_edges"
    __table_args__ = (UniqueConstraint("kb_id", "src", "dst", "chunk_id",
                                       name="uq_gedge_pair_chunk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True)
    src: Mapped[str] = mapped_column(String(64), index=True)
    dst: Mapped[str] = mapped_column(String(64), index=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    chunk_id: Mapped[int] = mapped_column(Integer, index=True)
    doc_id: Mapped[int] = mapped_column(Integer, index=True)


class PromptRecord(Base):
    """Prompt 中心（docs/05 §3）：Prompt 是独立资产——模板/变量/版本。

    PromptRecord 为「当前发布指针」；历史版本在 PromptVersionRecord（版本流水线）。
    """

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(String(256), default="")
    template: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[list] = mapped_column(JSON, default=list)  # 自动提取 {{var}}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PromptVersionRecord(Base):
    """Prompt 版本流水线（docs/05 §3，M3）：draft→published→archived，支持回滚。"""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    template: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # draft | published | archived
    notes: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PromptExperimentRecord(Base):
    """Prompt A/B 实验（docs/05 §3，M3）：按 key 稳定 hash 在两个版本间分流。"""

    __tablename__ = "prompt_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prompt_name: Mapped[str] = mapped_column(String(64), index=True)
    version_a: Mapped[str] = mapped_column(String(32))
    version_b: Mapped[str] = mapped_column(String(32))
    percent_b: Mapped[int] = mapped_column(Integer, default=50)  # 0-100
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvalDatasetRecord(Base):
    """评测数据集：规则裁判用例（input + expected_any 关键词）。"""

    __tablename__ = "eval_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(256), default="")
    cases: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvalRunRecord(Base):
    """评测运行：逐用例结果 + 通过率 + 门禁结论（docs/08 §3）。

    judge=rule：expected_any 关键词命中；judge=llm：LLM-as-Judge 按评分标准裁判。
    """

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(8), default="PENDING")  # PASS | FAIL | PENDING
    min_pass_rate: Mapped[float] = mapped_column(default=0.8)
    judge: Mapped[str] = mapped_column(String(8), default="rule")  # rule | llm
    scores: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MCPServerRecord(Base):
    """MCP Registry：外部 MCP Server 纳管（docs/04 §5）。"""

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(256))
    header_name: Mapped[str] = mapped_column(String(64), default="Authorization")
    api_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tools: Mapped[list] = mapped_column(JSON, default=list)  # 上次验证时发现的工具名
    status: Mapped[str] = mapped_column(String(16), default="registered")  # registered|verified|unreachable
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryRecord(Base):
    """记忆体系（docs/03 §4）：会话历史 / 长期记忆 / 摘要，可检索、可遗忘。"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)  # session | user
    kind: Mapped[str] = mapped_column(String(16), default="fact")  # message | fact | preference | summary
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    agent: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmbedChannel(Base):
    """嵌入外链渠道（docs/04 §6）：EmbedToken 绑定 agent + 域名白名单。

    开发版明文存 token；生产应只存哈希并在 KMS 托管签名密钥（docs/06 §1）。
    """

    __tablename__ = "embed_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(64), index=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    domains: Mapped[list] = mapped_column(JSON, default=list)  # ["*"] 或域名列表
    status: Mapped[str] = mapped_column(String(16), default="enabled")  # enabled | disabled
    note: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentReleaseRecord(Base):
    """Agent 发布治理（docs/06 §2，M3）：版本生命周期 Draft→Review→Prod→Rollback。

    - 评测门禁：进入 prod 必须绑定一条 PASS 的评测运行（EvalRunRecord）
    - prod 指针：同一时刻每个 agent 至多一条 state=prod 的发布（提升时自动退役旧版）
    """

    __tablename__ = "agent_releases"
    __table_args__ = (UniqueConstraint("agent", "version", name="uq_release_agent_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # draft | review | prod | rolled_back | retired
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eval_verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)  # PASS | FAIL
    canary_percent: Mapped[int] = mapped_column(Integer, default=0)  # 0-100，仅 canary 态生效
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)  # canary 覆盖，MVP: {"model": "..."}
    notes: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

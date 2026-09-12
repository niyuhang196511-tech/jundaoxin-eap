"""ORM 领域模型 —— docs/05 领域模型的 M1 子集。

生产环境需加 tenant_id 列 + PostgreSQL RLS（docs/05 §5），此处为开发版。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
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


class PromptRecord(Base):
    """Prompt 中心（docs/05 §3）：Prompt 是独立资产——模板/变量/版本。"""

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(String(256), default="")
    template: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[list] = mapped_column(JSON, default=list)  # 自动提取 {{var}}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
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
    """评测运行：逐用例结果 + 通过率 + 门禁结论（docs/08 §3）。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(8), default="PENDING")  # PASS | FAIL | PENDING
    min_pass_rate: Mapped[float] = mapped_column(default=0.8)
    scores: Mapped[list] = mapped_column(JSON, default=list)
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

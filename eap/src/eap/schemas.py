"""Pydantic 请求/响应模型 + Agent Manifest（docs/07 §4 的 M1 子集）。"""

from __future__ import annotations


from pydantic import BaseModel, Field, field_validator

ALLOWED_CAPABILITIES = {
    "chat", "reasoning", "embedding", "rerank", "vision",
    "extraction", "stt", "tts", "image_gen", "moderation",
}


# ---------- OpenAI 兼容 ----------

class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "auto"  # auto = 按能力路由
    messages: list[ChatMessage]
    tools: list[dict] | None = None
    stream: bool = False
    temperature: float = 0.7


# ---------- 知识中心 ----------

class KBCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    title: str = ""
    template: str = "doc"  # doc | faq
    # RAG pipeline 组件选型（扩展开发体系）：{"chunker": {"name": str, "params": {}}, ...}
    pipeline: dict = {}


class DocIngest(BaseModel):
    title: str
    text: str = Field(min_length=1)
    source: str = ""


class FAQItem(BaseModel):
    question: str
    answer: str


class FAQIngest(BaseModel):
    items: list[FAQItem] = Field(min_length=1)


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    rerank: str | None = Field(default=None, pattern=r"^(lexical|llm)$",
                               description="两阶段重排：lexical=词面覆盖度；llm=模型重排（失败回退 lexical）")


class Citation(BaseModel):
    kb: str
    document: str
    chunk_id: int
    chunk_index: int = 0


class RetrieveHit(BaseModel):
    content: str
    score: float
    citation: Citation


class RetrieveResponse(BaseModel):
    kb: str
    hits: list[RetrieveHit]


# ---------- 模型中心 ----------

class ModelRegister(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9.-]{2,60}$")
    capabilities: list[str]
    provider: str = "mock"  # mock | openai_compat
    base_url: str | None = None
    api_key: str | None = None
    remote_model: str | None = None
    priority: int = 100
    notes: str = ""

    @field_validator("capabilities")
    @classmethod
    def check_caps(cls, v: list[str]) -> list[str]:
        bad = set(v) - ALLOWED_CAPABILITIES
        if bad:
            raise ValueError(f"未知能力: {bad}，允许: {sorted(ALLOWED_CAPABILITIES)}")
        if not v:
            raise ValueError("至少声明一个能力")
        return v


# ---------- Agent Registry ----------

class ModelNeed(BaseModel):
    capability: str
    required: bool = True

    @field_validator("capability")
    @classmethod
    def check_cap(cls, v: str) -> str:
        if v not in ALLOWED_CAPABILITIES:
            raise ValueError(f"未知能力: {v}")
        return v


class AgentManifest(BaseModel):
    """K8s 风格能力声明（docs/03 §7.3）：声明即权限边界，版本即灰度单位。"""

    apiVersion: str = "agent.eap.io/v1"
    kind: str = "AgentApp"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = ""
    models: list[ModelNeed] = Field(default_factory=lambda: [ModelNeed(capability="chat")])
    knowledge: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)  # 多智能体：可委派的下级智能体
    permissions: list[str] = Field(default_factory=list)
    embeddable: bool = False
    domains: list[str] = Field(default_factory=list)
    # v0.5：结构化输出 / 交互 UI Schema 的代码级默认声明（DB 配置版本可覆盖）
    output_schema: dict | None = None
    interaction_schema: dict | None = None

    @field_validator("kind")
    @classmethod
    def check_kind(cls, v: str) -> str:
        if v != "AgentApp":
            raise ValueError("kind 必须为 AgentApp")
        return v


# ---------- Agent 调用 ----------

class InvokeRequest(BaseModel):
    input: str = Field(min_length=1)
    stream: bool = False
    session_id: str | None = None  # 会话记忆键（多轮上下文，docs/03 §4）
    user_id: str | None = None  # 长期记忆归属
    output_schema: dict | None = Field(
        default=None, description="结构化输出 JSON Schema（v0.5，请求级，优先于配置版本/代码默认）")


class InvokeResult(BaseModel):
    """AgentApp.on_invoke 的统一返回（docs/03 §7）。"""

    content: str
    citations: list[Citation] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    data: dict | None = None  # 结构化输出（v0.5）：schema 校验通过的 JSON 对象
    data_schema: dict | None = None  # 生成 data 所用的 schema（前端渲染提示）


class InvokeResponse(BaseModel):
    invocation_id: str
    agent: str
    agent_version: str
    trace_id: str
    output: str
    citations: list[Citation] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    canary: dict | None = None  # 命中灰度时：{"release_id","version","percent"}（docs/06 §2）
    config_version: str | None = None  # 命中的配置版本（Agent 配置版本层，v0.5）
    data: dict | None = None  # 结构化输出（v0.5）
    data_schema: dict | None = None

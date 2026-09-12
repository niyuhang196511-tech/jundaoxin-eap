# EAP 平台代码（M1 核心）

企业级 Agent 智能体平台的可运行核心：**模型中心 / 知识中心 / Agent Runtime / 注册钩子 SDK / OpenAI 兼容 API**。
架构设计见上级目录 [docs/](../docs/)（三面七层、领域模型、API 规范、路线图）。

## 环境与启动（uv + Python 3.12）

```bash
cd eap
uv sync                 # 创建 .venv 并锁定依赖（自动使用 Python 3.12）
uv run pytest           # 运行测试
uv run python -m eap    # 启动平台（默认 http://0.0.0.0:8300）
```

默认完全离线可运行（mock 模型 + hash 嵌入 + SQLite + 种子数据）。
接入真实供应商：

```bash
EAP_OPENAI_BASE_URL=https://api.deepseek.com/v1 \
EAP_OPENAI_API_KEY=sk-xxx \
EAP_OPENAI_MODEL=deepseek-chat \
uv run python -m eap
```

## 冒烟验证

```bash
KEY="Authorization: Bearer dev-key-1"

# 健康检查（含各智能体状态）
curl -s localhost:8300/health

# OpenAI 兼容对话（模型中心自动路由；配了外部供应商则优先）
curl -s -H "$KEY" localhost:8300/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'

# 知识库混合检索（BM25+向量 RRF 融合，带 Citation）
curl -s -H "$KEY" localhost:8300/api/v1/kb/website-faq/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query":"如何注册手写智能体","top_k":2}'

# 智能体调用（注册钩子纳管的 faq-agent）
curl -s -H "$KEY" localhost:8300/api/v1/agents/faq-agent/invocations \
  -H "Content-Type: application/json" \
  -d '{"input":"如何创建知识库？"}'
```

## 嵌入外链（把智能体挂到任意网站）

```bash
# 1. 创建嵌入渠道 → 得到 EmbedToken（绑定 agent + 域名白名单）
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" \
  localhost:8300/api/v1/agents/faq-agent/embed \
  -d '{"domains":["company.cn","*.company.cn"],"note":"官网客服"}'

# 2. 第三方页面后端用 EmbedToken 换短时会话（校验 Origin 白名单 + 频控）
curl -s -X POST localhost:8300/api/v1/embed/session \
  -H "Authorization: Bearer eap_emb_xxx" -H "Origin: https://company.cn" \
  -H "Content-Type: application/json" -d '{"user_id":"visitor-1"}'
```

页面接入（三行代码，Web Component + SSE 流式 + 引用标签）：

```html
<script src="https://your-eap-host/sdk/eap-widget.js"></script>
<eap-chat agent="faq-agent" endpoint="https://your-eap-host"
          session="（后端换取的会话令牌；开发演示可直接用 token 属性）"></eap-chat>
```

本地演示：启动后打开 **http://localhost:8300/sdk/demo.html**（种子渠道 `dev-embed-token`，域名不限）。

安全语义：会话令牌（`eap_sess_`，HMAC 签名 + 过期）**只能调用绑定的那一个智能体**，KB 检索/模型管理/chat 端点一律 403（`require_api_key` 最小权限边界）；伪造签名 401、停用渠道即时失效、域名不在白名单 403。

# 模型列表 / 注册定制模型
curl -s -H "$KEY" localhost:8300/api/v1/models
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/models \
  -d '{"name":"my-lora","capabilities":["chat"],"provider":"openai_compat","base_url":"http://gpu-host:8000/v1","api_key":"x","remote_model":"my-lora","priority":5}'

# 多智能体：support-supervisor 按领域把问题委派给 faq/order 子智能体（深度护栏 3 层）
curl -s -H "$KEY" localhost:8300/api/v1/agents/support-supervisor/invocations \
  -H "Content-Type: application/json" \
  -d '{"input":"帮我查一下订单 1001 的状态，顺便告诉我退货政策"}'

# 记忆：写入 → 召回 → 遗忘
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/memory \
  -d '{"scope":"user","user_id":"u-1","content":"客户偏好周一下单，走顺丰","kind":"preference"}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/memory/recall \
  -d '{"scope":"user","user_id":"u-1","query":"物流偏好","top_k":3}'

# MCP Registry：纳管外部 MCP Server（校验可达后启用，其工具注入平台工具池）
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/mcp/servers \
  -d '{"name":"company-tools","url":"https://mcp.corp.cn","header_name":"Authorization","api_key":"sk-x"}'

# 发布治理：登记 → 门禁评测 → 提升 prod → 回滚
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/releases \
  -d '{"agent":"faq-agent","version":"1.1.0","notes":"新增退货政策问答"}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/releases/<id>/eval \
  -d '{"dataset":"faq-smoke","min_pass_rate":0.8}'   # FAIL 则提升被 403 拦截
curl -s -X POST -H "$KEY" localhost:8300/api/v1/releases/<id>/promote
curl -s -H "$KEY" localhost:8300/api/v1/releases/current/faq-agent
curl -s -X POST -H "$KEY" localhost:8300/api/v1/releases/<id>/rollback

# 成本中心：设月度 token 预算 → 用量汇总 → 超限后调用被 429 熔断
curl -s -X PUT -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/budgets \
  -d '{"tenant_id":1,"monthly_token_budget":1000000}'
curl -s -H "$KEY" localhost:8300/api/v1/budgets/1            # 预算+当月用量+是否熔断
curl -s -H "$KEY" localhost:8300/api/v1/budgets/1/summary    # 按 kind/model 分组明细

# 企业连接器：种子 mock-erp 已内置；登记 REST 连接器 → 验证 → 查看注入的工具
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/connectors \
  -d '{"name":"corp-erp","kind":"rest","base_url":"https://erp.corp.cn","api_key":"x","endpoints":[{"name":"order.create","tool_name":"erp.order.create","method":"POST","path":"/orders","requires_approval":true}]}'
curl -s -X POST -H "$KEY" localhost:8300/api/v1/connectors/corp-erp/validate
curl -s -H "$KEY" localhost:8300/api/v1/connectors/mock-erp/tools
# order-agent 自动改用连接器 ERP：库存查询走 erp.inventory.query，下单仍触发 HITL 审批
```

## 手写智能体接入（注册钩子）

```python
from eap.agents.app import AgentApp
from eap.agents.manifest import AgentManifest
from eap.agents.registry import register_agent
from eap.schemas import InvokeRequest, InvokeResult

@register_agent(AgentManifest(name="my-agent", version="1.0.0", knowledge=["product-docs"]))
class MyAgent(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        with self.ctx.db() as db:
            hits = self.ctx.retriever("product-docs").search(db, request.input)
            completion = await self.ctx.chat(db, messages=[{"role": "user", "content": request.input}],
                                             system=self.ctx.retriever.render(hits))
            return InvokeResult(content=completion.result.content,
                                citations=[h["citation"] for h in hits])
```

放置到任意模块并配置 `EAP_AGENT_MODULES='["your.module"]'`，平台启动即自动发现、校验、纳管（pip 安装包用 entry_points 组 `eap.agents`）。完整示例见 [examples/demo_agent/](examples/demo_agent/)。

## 已实现 vs 待实现

| 模块 | 已实现 | 待实现（见 docs/09 路线） |
|---|---|---|
| 模型中心 | 能力路由、降级链、mock/OpenAI 兼容、定制模型注册 API | Policy Engine、评测门禁接入路由、vLLM multi-LoRA 托管 |
| 知识中心 | 多 KB、分块、hash/openai 嵌入、BM25+向量+RRF、Citation、级联删除 | 图谱路（LightRAG）、Reranker、Milvus |
| Agent Runtime | Context 预算、Agent Loop、工具协议、HITL 审批门控 + 挂起/恢复 | 人工审批 UI、持久化 Checkpoint |
| **多智能体** | **Supervisor 委派（agent.\* 工具化）+ 深度护栏 + 内置 supervisor-agent** | Handoff 策略编排 UI、跨租户 A2A 委派 |
| **Memory** | **会话/长期记忆读写、向量+词面召回、遗忘 API（/api/v1/memory）** | 摘要压缩、组织记忆联动知识中心 |
| **Workflow Engine** | **DSL（llm/tool/retrieve/branch 节点 + 条件跳转）→ 创建即注册为智能体** | 画布前端、并行节点、子流程 |
| **Prompt Center** | **模板/变量自动提取/渲染/API**，工作流与智能体引用 | 版本流水线、A/B 实验 |
| **评测中心** | **数据集 + 规则裁判 + 通过率门禁**（发布门禁语义就位） | LLM-as-Judge、人工抽检、在线影子流量 |
| **发布治理** | **版本生命周期（draft→review→prod→rolled_back/retired）+ 评测门禁强制 + 回滚恢复旧版（/api/v1/releases）** | Canary 灰度比例、环境体系（Dev/Staging/Prod）、制品组合版本 |
| **成本中心** | **租户月度 token 预算 + 用量汇总（按 kind/model 分组）+ 调用侧超限熔断 429（/api/v1/budgets）** | 单价计费账单、按模型差价、配额分层 |
| **企业连接器** | **连接器注册/验证/启停 API + 端点→平台工具自动包装（rest + 内置 mock-erp）+ order-agent 经连接器调 ERP（/api/v1/connectors）** | SQL 连接器、飞书/钉钉、OAuth 凭证托管 |
| Task/Job 引擎 | 状态机、队列+Worker、取消/审批/续跑 | Redis Streams、定时调度 |
| Skill Registry | 技能 CRUD、L1/L2 渐进披露、启停、Agent 注入 | SKILL.md 打包/签名、技能市场 |
| MCP | Server（/mcp，官方 SDK 2.x）+ Client（外部 Server → 平台工具）+ **Registry（Server 纳管/验证/启停 API）** | OAuth、长连接复用 |
| 注册 SDK | @register_agent、manifest 校验、entry_points 发现、健康检查 | 生命周期全钩子、热加载、灰度 |
| 接入 | OpenAI 兼容（含 SSE）、Agent 调用、KB API、嵌入外链（EmbedToken+JS Widget）、**A2A 1.0（Agent Card + Task）**、**React 控制台（总览/模型/知识/智能体/任务/资产/评测）** | OIDC/SSO、多租户计费 |

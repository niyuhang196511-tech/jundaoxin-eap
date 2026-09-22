# EAP 平台代码（v1.0.0）

企业级 Agent 智能体平台可运行工程（v1.0.0）：**模型 / 知识 / Agent Runtime / Workflow（版本化+四环境）/ 交互引擎 / 发布治理 / 扩展平台 / 企业集成（事件-Webhook-IM-A2A-网关）/ 生产化（Worker 池-沙箱-HA-CI/CD）**。
架构设计见上级目录 [docs/](../docs/)（三面七层、领域模型、API 规范、路线图）。

## 环境与启动（uv + Python 3.12）

```bash
cd eap
uv sync                 # 创建 .venv 并锁定依赖（自动使用 Python 3.12）
uv run pytest           # 运行测试
uv run python -m eap    # 启动平台（默认 http://0.0.0.0:8300）
```

默认完全离线可运行（mock 模型 + hash 嵌入 + SQLite + 种子数据）。

**一键导览**（进程内走通 12 个模块：模型/知识/智能体/多智能体/Workflow/记忆/发布治理+灰度/成本/策略/连接器+HITL/技能包/Prompt A/B，无需启动服务）：

```bash
uv run python scripts/demo.py
```

接入真实供应商：

```bash
EAP_OPENAI_BASE_URL=https://api.deepseek.com/v1 \
EAP_OPENAI_API_KEY=sk-xxx \
EAP_OPENAI_MODEL=deepseek-chat \
uv run python -m eap
```

## Docker 部署（单机）

```bash
cd eap
cp .env.example .env        # 更换 API Key / SESSION_SECRET / POSTGRES 口令
docker compose up -d --build
curl localhost:8300/health  # {"status":"ok",...}
```

- 镜像多阶段构建：Node 构建控制台 → Python 3.12 运行时（uv 锁定依赖、非 root、健康检查）
- PostgreSQL 16 持久化（`postgres` extra 提供驱动），启动自动建表 + 种子
- 生产检查清单见 docker-compose.yml 注释；部署验证冒烟：
  `docker compose exec -T eap python -` 后粘贴 docs/10 附录的冒烟脚本（管道输入）
- 完整配置项说明见 [.env.example](.env.example)

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
# Canary 灰度：review 态发布先进灰度（需 PASS 门禁），按 user/session 稳定 hash 分流
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/releases/<id>/canary \
  -d '{"percent":10,"overrides":{"model":"my-lora"}}'   # 命中的调用响应带 canary 标记
curl -s -X POST -H "$KEY" localhost:8300/api/v1/releases/<id>/promote   # 灰度满意 → 全量 prod
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

# Policy Engine：租户策略在模型网关强制执行（租户策略存在时平台默认不叠加）
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/policies \
  -d '{"name":"local-only","tenant_id":1,"kind":"provider-allowlist","config":{"providers":["mock"]},"priority":10}'
# → 该租户调用只路由到本地供应商（数据不出域）；违规返回 403 EAP-7101
curl -s -H "$KEY" "localhost:8300/api/v1/policies?tenant_id=1"

# 企业 IM：登记渠道（飞书/钉钉/企业微信）→ 回调端点挂到 IM 开放平台 → 群里 @机器人 即问即答
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/im/channels \
  -d '{"name":"fs-1","platform":"feishu","agent":"faq-agent","secret":"verification-token","webhook_url":"https://open.feishu.cn/open-apis/bot/v2/hook/xxx"}'
# 把回调地址配到飞书/钉钉/企微后台（钉钉加签、企微需 token+EncodingAESKey 存入 extra）
curl -s -X POST -H "$KEY" localhost:8300/api/v1/im/channels/fs-1/test   # 推送连通性冒烟

# 评测中心：规则裁判之外支持 LLM-as-Judge（expectation 为自然语言评分标准）
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/evals/datasets \
  -d '{"name":"answer-quality","cases":[{"input":"如何退货？","expectation":"回答需说明退货流程与时效"}]}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/evals/runs \
  -d '{"agent":"faq-agent","dataset":"answer-quality","judge":"llm","min_pass_rate":0.8}'
# → 逐用例返回 {"passed":bool,"reason":"理由"}；发布门禁 /eval 同样支持 judge=llm

# Prompt 版本流水线 + A/B：建草稿版 → 发布（试渲染校验）→ 按 session/user 稳定分流 → 可回滚
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/prompts/faq-answer-style/versions \
  -d '{"version":"1.1.0","template":"资深客服模式：{{question}}","notes":"结构化改版"}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/prompts/faq-answer-style/publish \
  -d '{"version":"1.1.0","variables_sample":{"question":"样例"}}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/prompts/experiments \
  -d '{"name":"style-exp","prompt":"faq-answer-style","version_a":"1.0.0","version_b":"1.1.0","percent_b":20}'
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" localhost:8300/api/v1/prompts/faq-answer-style/render \
  -d '{"variables":{"question":"怎么退货"},"key":"session-42"}'   # 返回命中版本 + 实验信息
curl -s -X POST -H "$KEY" localhost:8300/api/v1/prompts/faq-answer-style/rollback

# 技能包（技能市场地基）：导出签名包 → 导入（验签失败 401 EAP-8101；导入默认停用待审）
curl -s -H "$KEY" localhost:8300/api/v1/skills/customer-service/package > skill.bundle.json
curl -s -H "$KEY" localhost:8300/api/v1/skills/public-key    # 公钥分发：Harness 本地验签
curl -s -X POST -H "$KEY" -H "Content-Type: application/json" \
  localhost:8300/api/v1/skills/import -d "{\"bundle\": $(cat skill.bundle.json)}"
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

## 功能全景与完成状态

权威进度账本：[docs/progress-plan.md](../docs/progress-plan.md)——14 个能力域 × 状态、逐项完成描述（含关键文件证据）、剩余工作与验收标准、并行任务分组。

版本路线：**v0.5 Agent Application / v0.6 Platform Governance / v0.7 Extension Platform / v0.8 企业集成 / v0.9 生产化 / v1.0 收官均已落地**（里程碑 M18–M35，收版审计 docs/12/13/15/16）；长期悬置项（vLLM multi-LoRA、Harness 桌面端、IM 真实凭证联调等）见账本 L 组。

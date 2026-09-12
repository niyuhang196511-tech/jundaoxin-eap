# 07 API / SDK / Protocol 规范

> EAP 系列文档 07/09 ｜ 上篇：[06 安全与治理](06-安全与治理架构.md) ｜ 下篇：[08 观测评测成本](08-观测评测成本与SLO.md)

---

## 1. 凭证体系（三种，勿混用）

| 凭证 | 签发 | 用途 |
|---|---|---|
| 用户 Token | OIDC/SSO 登录 | 控制台、Harness 用户身份 |
| Agent API Key | 控制台/CI 签发，绑定 Agent 与权限 | 服务端调用、SDK 注册的 Agent 自身调用 |
| Embed Token | 渠道创建时签发 | 仅限嵌入外链，绑定 agent+域名+租户，短时效可刷新 |

## 2. 对外 API

### 2.1 OpenAI 兼容端点（模型代理模式）

```
POST /v1/chat/completions        # 转发模型网关：模型路由/降级/计量生效
POST /v1/embeddings
```

### 2.2 Agent 调用端点

```
POST /api/v1/agents/{agent}/invocations          # 同步调用（阻塞完成）
POST /api/v1/agents/{agent}/invocations:stream   # SSE 流式（token 级 + 事件级）
POST /api/v1/agents/{agent}/tasks                # 长任务（异步，返回 task_id）
GET  /api/v1/tasks/{task_id}                     # 任务状态/结果/产物
POST /api/v1/tasks/{task_id}:cancel
POST /api/v1/tasks/{task_id}:approve             # HITL 审批（payload: 决策+意见）
GET  /api/v1/agents/{agent}/card                 # A2A Agent Card
```

统一信封：`{ invocation_id, agent_version, output, citations[], usage{tokens,cost}, trace_id }`；错误码 `EAP-1xxx(鉴权) / 2xxx(限流) / 3xxx(策略拒绝) / 4xxx(执行) / 5xxx(依赖)`。

### 2.3 知识检索端点

```
POST /api/v1/kb/{kb_id}/retrieve   { query, top_k, filter, with_citation: true }
→ { hits: [ {content, score, citation{doc,page,chunk,kb_version}} ] }
```

## 3. MCP 与 A2A 端点

| 端点 | 协议 | 说明 |
|---|---|---|
| `/mcp/platform` | MCP (Streamable HTTP + OAuth) | 平台 MCP Server：工具、`kb.search`、`agent.chat` 等 |
| `/mcp/registry` | REST | MCP Server 纳管（server.json 注册/校验/启停） |
| `/.well-known/agent-card.json` | A2A 1.0 | 每个已发布 Agent 的名片 |
| `/a2a/v1` | A2A Task/Message | 平台作为 A2A Server 接受外部 Agent 委派；平台作为 A2A Client 调外部 |

## 4. Agent Manifest 规范

完整 Schema（要点版，机器可校验的 JSON Schema 随 SDK 发布）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `apiVersion` / `kind` | ✓ | `agent.eap.io/v1` / `AgentApp` |
| `metadata.name/version/publisher` | ✓ | name 全局唯一；semver |
| `spec.runtime` | ✓ | python/sdk 版本约束 |
| `spec.entry` | SDK 形态必填 | `module:Class` |
| `spec.models[]` | ✓ | 按 capability 声明（reasoning/vision/extraction…），不写死模型名 |
| `spec.knowledge[]` | | KB id 列表（权限即边界） |
| `spec.tools[]` / `spec.skills[]` | | 工具/技能引用 |
| `spec.permissions[]` | ✓ | 声明式权限，策略中心审计 |
| `spec.resources` / `spec.network.allow` | | 资源配额 / 出网白名单 |
| `spec.endpoints` | | 缺省 auto（平台生成 invoke/stream/health） |
| `spec.embeddable` | | 外链开关 + 域名白名单 |

## 5. platform-sdk（Python）接口签名

```python
from eap_sdk import register_agent, AgentApp, AgentManifest, platform
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph

manifest = AgentManifest.from_yaml("agent.yaml")   # 即 07 §4 的 Manifest

@register_agent(manifest)                          # 注册钩子：生命周期由平台驱动
class SalesAgent(AgentApp):

    async def on_start(self):
        # ① 模型：OpenAI 兼容网关（路由/降级/计量自动生效）
        self.llm = ChatOpenAI(base_url=platform.gateway_url,
                              api_key=platform.agent_key, model="auto")

        # ② 知识库：标准 LangChain Retriever / 直接变 Tool
        self.retriever = platform.retriever("product-docs").as_langchain_retriever(k=5)
        kb_tool = platform.retriever("sales-playbook").as_tool()

        # ③ 动态技能与工具：启动时按 manifest 经 MCP 适配器拉取
        #    平台侧新增/下线技能，本 Agent 无需改代码重启即生效
        self.dynamic_tools = await platform.mcp_tools()      # langchain-mcp-adapters

        # ④ LangGraph 图（标准写法，checkpointer 桥接平台 State Manager）
        self.graph: StateGraph = build_graph(
            self.llm, self.retriever, [kb_tool, *self.dynamic_tools],
            checkpointer=platform.checkpointer(),            # Postgres 后端
        )

    async def on_invoke(self, req: InvokeRequest) -> InvokeResponse:
        result = await self.graph.ainvoke(req.input, config=req.config)
        return InvokeResponse(output=result, citations=result.citations)

    async def on_stream(self, req): ...                    # SSE 事件流
    async def health_check(self) -> Health: return Health.ok()
```

**不锁定框架**：任何能暴露 HTTP 的手写服务，登记 `/.well-known/agent-card.json`（A2A）或 OpenAPI 文档即可被 Registry 自动发现纳管；SDK 路径只是体验最优解。

## 6. 嵌入外链 JS SDK

```html
<script src="https://eap.company.cn/sdk/eap-widget.js"></script>
<eap-chat agent="website-faq-agent" endpoint="https://eap.company.cn"
          user-id="visitor-123" theme="auto"></eap-chat>

<script>
  // 可编程模式：先在服务端用 EmbedToken 换会话（避免前端暴露长期凭证）
  EAP.widget.mount("#chat", {
    agent: "website-faq-agent",
    session: "（后端返回的短时会话令牌）",
    onEvent: (e) => console.log(e.type)   // message / tool_call / citation
  });
</script>
```

Web Component（Shadow DOM 隔离样式）+ 可编程双模式；SSE 流式；断线自动重连（会话幂等）。

## 7. 事件与 Webhook

`agent.registered / agent.unhealthy / task.completed / task.waiting_human / guardrail.blocked / budget.exceeded / model.canary_failed` → Webhook / 飞书·钉钉·企微机器人推送。所有事件带 `tenant_id + trace_id`。

## 8. 幂等与限流

- 幂等键：调用方传 `Idempotency-Key`，Invocation 级去重（24h 窗口）；
- 限流：网关双层（租户 QPS + Agent 并发），模型层另有供应商 RPM/TPM 池化配额；
- 降级语义：网关过载返回 `EAP-2001` + `Retry-After`；SDK 自动退避。

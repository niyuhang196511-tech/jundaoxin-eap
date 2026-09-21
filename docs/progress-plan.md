# EAP 完成计划列表（Progress Plan）

> **用途**：本文件是平台功能的**持久化进度账本**——每项功能带状态、完成描述、剩余工作、验收标准（DoD）、依赖与并行分组。后续开发**照单取任务，完成后回写状态与 commit**，避免每次全库重新探查。
>
> **来源**：基于 [unfinished.md](./unfinished.md)（v1.0 功能全景设计文档）与 v0.7.0 树（b025ecf）实际代码逐项核对生成。
>
> **维护约定**：
> ① 每完成一项 → 把状态改为 ✅、补一行完成描述并附 commit 号；
> ② 新需求追加到对应任务组，不另起文档；
> ③ 每轮开发从「优先级最高且依赖已满足」的任务组取任务，组内各线可**并行推进**；
> ④ 每次收版（chore(release)）同步更新快照头；
> ⑤ 状态图例：✅ 已完成 ｜ 🔶 部分完成（缺项已列）｜ ❌ 未启动。

---

## 快照头

| 项 | 值 |
|---|---|
| 平台版本 | v0.7.0 + M30 批次 1（event/im/a2a 三线，见下） |
| 最新里程碑 | M30（批次 1：event-M30 / im-M30 / a2a-M30） |
| 代码 commit | bff9797 |
| 快照日期 | 2026-09-21 |
| 测试基线 | **234 passed, 6 skipped, 0 errors**（263s）——较 M29 基线 198 passed 增 36（恰为批次 1 三线新用例 8+13+15）；M29 时 3 个 test_mcp 的 Windows 回环防火墙 error 本轮未复现（间歇性环境问题，docs/13 §三） |
| 下一主线 | **批次 2：B `webhook-` + C `connector-` + F `gateway-`**（M31；B 依赖的 A 事件总线已就绪） |
| 审计状态 | docs/12（v0.5）/ docs/13（v0.6）/ docs/15（v0.7）三轮收版扫描；遗留 5 个 MinerU SSRF 维持「补偿控制在位、可接受」判定 |

---

## 一、版本路线总览（unfinished.md §五十）

| 版本 | 主题 | 状态 | 里程碑 | 收版审计 |
|---|---|---|---|---|
| v0.5 | Agent Application（版本层/结构化输出/交互引擎/Action/Artifact） | ✅ 完成 | M18–M22 | docs/12 |
| v0.6 | Platform Governance（工具治理/Policy/评测/成本/Memory 治理） | ✅ 完成 | M23–M26 | docs/13 |
| v0.7 | Extension Platform（统一 Manifest/注册表/Bundle/SDK 契约） | ✅ 完成 | M27–M29 | docs/15 |
| **v0.8** | **Enterprise Integration（Connector/Webhook/Event/IM/A2A/Discovery/Gateway）** | 🔶 进行中（Event/IM/A2A+Discovery 已完成 ✅；Webhook/Connector/Gateway 待批次 2） | M30 ✅ / M31 待开工 | — |
| v0.9 | Production（分布式 Worker/Sandbox/HA/环境体系/CI-CD） | ❌ 未启动（底座已备） | 排在 v0.8 后 | — |
| v1.0 | Enterprise Agent Platform 收尾整合 | ❌ | 最后 | — |

---

## 二、能力域总览（unfinished.md §一 ①~⑭）

| # | 能力域 | 状态 | 完成描述 / 缺口指向 |
|---|---|---|---|
| ① | Identity / Access | ✅ | JWT/OIDC/SSO/API Key 哈希/PostgreSQL RLS/RBAC 收口（`api/security.py`、`runtime/oidc.py`、`observability/rls.py`） |
| ② | Agent Center | ✅ | 配置版本层 draft→publish→archived + rollback + diff + 前端版本抽屉（M18，`runtime/agent_config.py`） |
| ③ | Agent Runtime | ✅ | Loop+HITL 审批+任务恢复+多智能体委派边界+交互挂起+结构化输出（`runtime/loop.py`、`tasks.py`、`multi_agent.py`、`interaction.py`） |
| ④ | Model Center | ✅ | 能力路由+降级链+真流式+定价计量+Mock 确定性合成（`modelhub/`）；vLLM multi-LoRA 悬置 → L1 |
| ⑤ | Knowledge / RAG | ✅ | 三路混合检索+Citation+可插拔组件+RAG 指标评测+MinerU/表格解析；文档级 ACL → L6 |
| ⑥ | Workflow | ✅ | DSL v2 图执行+拖拽画布+interaction 节点+变量快照挂起续跑（`runtime/workflow.py`）；版本化/环境体系 → P3 |
| ⑦ | Tool / MCP / Plugin | ✅ | Tool Governance（M24）+MCP Client/Server+插件热载+Extension Platform（M27–29） |
| ⑧ | Connector | 🔶 | 连接器记录+端点工具化+风险映射+secret 加密已有（`runtime/connectors.py`）；SQL/OAuth/Trigger/Health → **任务组 C** |
| ⑨ | Memory | ✅ | session/user 两层+租户过滤+保留期清理+批量遗忘/导出（M26，`runtime/memory.py`）；摘要压缩/组织层 → L7 |
| ⑩ | Interaction / UI Schema | ✅ | 9 控件+任务/聊天双通道挂起恢复+动态选项级联+自定义 UI 组件 SDK MVP（M20/M29） |
| ⑪ | Artifact / File | ✅ | 产物表+存储+预览/下载+过期惰性剔除+InvokeResult 引用（M21，`runtime/artifacts.py`） |
| ⑫ | Evaluation | ✅ | LLM 裁判多维评分+异步评测+回归对比+RAG 指标 HitRate/Recall/MRR/NDCG（M25） |
| ⑬ | Governance / Policy | ✅ | 6 种策略 kinds+工具治理三件套+成本计量报表+按凭证限流+审计查询页（M23–M26） |
| ⑭ | Observability | ✅ | 结构化日志+/metrics（Prometheus）+OTel tracing+全端点审计（`observability/`） |

---

## 三、已完成明细（v0.5–v0.7 证据索引）

### v0.5 Agent Application（M18–M22，收版 fb63c5e，168 passed）

| 设计项 | 里程碑 | 完成描述（关键证据） |
|---|---|---|
| ① Agent Registry / Version | M18 · 6a1770b | `agent_versions` 表 draft→publish→archived 流水线 + `published_version` 发布指针；运行时覆盖层经 ContextVar 下发（system_role / temperature / 工具白名单 / max_steps / 知识库白名单）；`/agents/{name}/versions` CRUD + publish/deprecate/rollback/diff API（admin+审计）；前端 Agents 版本抽屉。文件：`runtime/agent_config.py`、`api/v1/agents.py`、`migrations/…b3e7f2a9c4d1`、前端 `components/agents/VersionDrawer.tsx` |
| ② Structured Output | M19 · 255955d | `hub.complete(response_schema)`：格式指令注入 → JSON 提取（围栏/prose 容错）→ jsonschema 校验 → 失败带反馈重试一次 → 回退纯文本；openai_compat 走 `response_format json_schema`；优先级：请求级 output_schema > 配置版本 > 代码默认；`InvokeResult.data`；前端 SchemaRenderer 渲染。文件：`modelhub/router.py`、`schemas.py`、前端 `components/chat/renderers/SchemaRenderer.tsx` |
| ③④ Interaction Engine + AI UI Schema | M20 · 292929e / d5cea4b | 9 种控件（text/textarea/number/select/multiselect/radio/checkbox/confirmation/form）+ `data_source` 动态选项（tool 取值/级联/审计）；**双通道挂起恢复**：任务通道 WAITING_INPUT 快照 + interact 端点（镜像 approve）、聊天通道 interactions 表 + submit 端点恢复协议；`ctx.interact` 两态通用；options 端点实时级联取值；内置 warehouse-agent 演示；前端 UISchemaRenderer（必填校验+级联重取）+ ChatPanel 内联表单提交续流 + Tasks 视图填表入口。文件：`runtime/interaction.py`、`migrations/…c5d8f1a2e6b4`、前端 `components/chat/UISchemaRenderer.tsx` |
| ⑤ User Interaction 工作流节点 | M22 · 863cdd3 | DSL `Step.type=interaction`（内联 ui_schema/input_var，parallel/loop 体内禁用）；WorkflowSuspended 变量快照落库 + pending_node；resume_node/resume_values 续跑 + 聊天通道 skip 模式；`POST /workflows/runs/{id}/submit`；workflow-as-agent 挂起 → `InvokeResult.interaction` 与聊天协议统一；画布节点/表单编辑/waiting_input 徽标。文件：`runtime/workflow.py`、`api/v1/workflows.py`、`migrations/…e9f3b6d2a7c5`、前端 `components/canvas/*` |
| ⑥ Action | M21 · ace275a | `runtime/actions.run_action`：UI 动作经 resolve_tool 统一解析（kb/工作流/连接器/插件）执行；requires_approval 高风险动作转 agent.hitl 既有审批流；action.invoke 审计 + 结果写会话记忆；`/api/v1/actions/invoke`；前端 x-actions 动作按钮。文件：`runtime/actions.py`、`api/v1/actions.py` |
| ⑦ Artifact | M21 · ace275a | artifacts 表 + `EAP_MEDIA_DIR/artifacts` 存储（文件名白名单+规范化越界防线）+ `ctx.artifacts.create` + /artifacts 列表/预览/下载（过期惰性剔除）+ `InvokeResult.artifacts` 引用。文件：`runtime/artifacts.py`、`api/v1/artifacts.py`、`migrations/…d7e2a4b8c1f3` |
| ⑧ Dynamic UI Data Source | M20 | data_source（type=tool）动态选项 + 级联重取 + 取值审计（并入 ③④ 交付） |

### v0.6 Platform Governance（M23–M26，收版 81ec0f2，186 passed）

| 设计项 | 里程碑 | 完成描述（关键证据） |
|---|---|---|
| ① Tool Governance | M24 · c827a2a | 工具 `risk_level`/`timeout_s` 元数据（连接器 requires_approval→risk=high）；策略三件套：tool-allowlist 白名单拒绝（PolicyDenied 回注模型）、tool-risk-approval 风险阈值强制审批、agent-allowlist 多智能体委派边界；每次工具调用审计 tool.call（脱敏）+ `eap_tool_calls_total` 指标 + `wait_for` 超时；task 通道策略上下文补齐（消除旁路）。文件：`runtime/policy.py`、`runtime/loop.py`、`api/v1/policies.py` |
| ② Policy Engine 扩展 | M24/M23 | 6 种策略 kinds + config 校验 + 前端配置模板（M23 修复 UI kind 与后端正则失配的存量 bug） |
| ③ Agent Evaluation | M25 · 56a2a33 | LLM 裁判多维评分（正确性/相关性/格式 1–5 分 + judge_model 配置）；评测改异步 `eval.run` 任务；运行历史+详情；回归对比（逐指标 delta + 劣化>1pt 标记）。文件：`api/v1/evals.py` |
| ④ RAG Evaluation | M25 · 56a2a33 | kind=rag 数据集（query + relevant_chunk_ids）+ HitRate@K/Recall@K/MRR/NDCG 确定性指标纯函数 + rag-runs 端点；Knowledge 召回测试面板「标注相关/不相关一键存为数据集」。文件：`runtime/rag_eval.py` |
| ⑤ Cost / Quota | M26 · 2eee165 | 模型定价 price_in/out（每百万 token，admin 注册配置）+ record_usage 计得 cost + UsageRecord agent 归属列；SSE 流式计量归属修复（原记 tenant 0 致预算不熔断）；`/budgets/{tenant}/report` 按模型/智能体/日聚合报表。文件：`modelhub/`、`api/v1/budgets.py`、`migrations/…a2c4e6f8b1d3` |
| ⑥ Permission-aware RAG | 🔶 部分 | KB/WorkflowRecord 补 tenant_id 列 + JWT 通道过滤真实生效（M23）；**文档级 ACL / 角色过滤未做** → L6 |
| ⑦ Memory Governance | M26 · 2eee165 | 写入归属租户 + recall/list/history 租户过滤（JWT 通道）；保留期清理（`EAP_MEMORY_RETENTION_DAYS` + /memory/purge）；按用户批量遗忘/导出（数据权利，admin+审计）。文件：`runtime/memory.py`、`api/v1/memory.py` |
| ⑧ Security 收口 | M23/M26 | RBAC 收口（budgets/releases 全写/plugins-reload 补 require_admin）；审计全端点补全 + 前端审计查询页（actor/target/时间过滤+分页）；IM secret Fernet 加密；限流按凭证 429+Retry-After。文件：`api/v1/audit.py`、前端 `views/AuditView.tsx` |

### v0.7 Extension Platform（M27–M29，收版 b025ecf，198 passed）

| 设计项 | 里程碑 | 完成描述（关键证据） |
|---|---|---|
| 统一 Extension Manifest | M27 · 612743a | 8 种扩展 type + `runtime.min_version` 平台兼容检查 + 旧 EAP_PLUGIN dict 归一化兼容。文件：`runtime/extension_manifest.py` |
| 持久扩展注册表 | M27 · 612743a | ExtensionRecord（registered/enabled/disabled/failed 跨重启保持，exposes/error 同步）；plugins.py 加载/热重载与注册表同步（disabled 不执行 register，重载先清注册副作用）；`GET /extensions/registry` + manifest 端点 + enable/disable（admin+审计）；前端扩展目录 Tab。文件：`plugins.py`、`migrations/…bd3e7f2a4c19` |
| .eapext Bundle 生命周期 | M28 · 656531c | 逐条目安全校验（拒绝绝对路径/`..`、文件名白名单、10MB/100 文件上限、resolve+is_relative_to 锁定）+ 安装/升级/卸载全流程 + API（admin+审计）；scaffold 统一 manifest 化 + pack 子命令。文件：`runtime/bundles.py`、`scaffold.py` |
| Extension SDK 契约 | M29 · 8583519 | `eap/ext` 包：RAG SDK（Chunker/Reranker 基类 + `__call__` 委托 + register 转发）、Workflow Node SDK（register_workflow_node）、Model Provider SDK（register_provider 注入路由链）、Connector SDK（register_connector_kind）、UI SDK MVP（register_ui_component）；docs/14 开发者指南。文件：`ext/sdk.py`、`docs/14-extension-sdk.md` |

---

## 四、未完成明细（照单实施区）

> 每组为一条可独立推进的功能线（分支前缀即组名）。组内工作串行、组间并行。
> 「现状」= 已有底座与证据；「剩余工作」= 完成描述；「DoD」= 验收标准。

### 主线：v0.8 企业集成（优先级 1，M30 起）

#### 任务组 A：`event-` 事件中心（M30，底座，先行开工）✅（M30，04cabca）
- **设计依据**：unfinished.md §二十八 Event/Trigger（Cron/Webhook/Event/Message/File Upload/Business Event）。
- **完成描述（M30，commit 04cabca）**：① 进程内事件总线 `runtime/events.py`（通配订阅、队列满丢弃不阻断、可选 Redis pub/sub 跨副本，离线纯进程内可跑）；② TriggerRule 三源触发 `runtime/triggers.py`（event/cron/webhook → agent/workflow 走既有任务通道，min_interval_s 限流 + trigger.fire 审计）+ `api/v1/triggers.py` CRUD（admin+审计，async def 保证 reload 在事件循环上）；③ 入站 webhook `POST /triggers/webhook/{id}`（HMAC-SHA256 签名 + X-EAP-Event-Id LRU 去重）；④ 事件发射点：agent.run.completed / task.completed|failed / workflow.run.finished / kb.document.indexed；迁移 `a3e5f7b9c1d2`；`tests/test_events_triggers.py` 8 用例离线绿。
- **现状**：定时触发已有（TaskScheduleRecord cron/interval，`runtime/tasks.py`，ops-M17.1/2）；任务队列/恢复/审计链完整；Webhook 仅有 IM 平台入站回调（`runtime/im.py`）；**无统一事件总线、无业务/文件事件、无入站 Webhook 触发器**。
- **剩余工作**：
  1. 进程内事件总线（发布/订阅；可选 Redis pub/sub 跨副本）+ 核心事件目录（`agent.run.completed`、`task.*`、`workflow.run.finished`、`kb.document.indexed`、`connector.*`）；
  2. TriggerRule 模型 + API：事件 / cron / 入站 webhook → 目标 agent/workflow（走既有任务通道，事件 payload 透传为输入）；
  3. 入站 webhook 触发端点（签名校验 + 去重）；
  4. 触发审计 + 触发限流（防风暴）；测试覆盖。
- **DoD**：配置「业务事件 → Agent 触发 → 产出 Artifact → 落审计」全链路测试绿；入站 webhook 带 HMAC 校验触发 agent；重复投递幂等。
- **依赖**：无（B、C-Trigger 的底座）。

#### 任务组 B：`webhook-` 对外 Webhook 推送（M31）
- **设计依据**：unfinished.md §三十五（接入方式：Webhook 出站）。
- **现状**：无出站 webhook；事件总线未建。
- **剩余工作**：① WebhookEndpoint 模型 + 管理 API（URL/secret/事件订阅列表，admin+审计）；② 事件 → HMAC-SHA256 签名推送 → 指数退避重试（复用任务引擎）→ 死信可见；③ 控制台配置页。
- **DoD**：订阅 `agent.run.completed` 后本地接收端收到带签名 payload；断连重试与死信可查询。
- **依赖**：A（事件总线）。

#### 任务组 C：`connector-` 连接器深化（M31）
- **设计依据**：unfinished.md §二十五 Connector Center（Authentication/Credential/Action/Trigger/Webhook/Health）。
- **现状**：ConnectorRecord + 端点工具化（`runtime/connectors.py`）+ requires_approval→risk=high（M24）+ secret Fernet（M26）；**缺 SQL 连接器、OAuth 凭证托管、Health Check、Trigger 联动**。
- **剩余工作**：① SQL 连接器 kind（只读白名单 + 超时 + 行数上限）；② OAuth2 凭证托管（client_credentials / authorization_code，令牌刷新 + 加密存储）；③ 连接器 Health Check + 状态透出；④ Trigger 注册进事件中心。
- **DoD**：sqlite demo 连接器建 Connection → 工具调用 → 审计；OAuth 令牌刷新测试绿。
- **依赖**：仅 ④ 依赖 A；①②③ 可即刻并行。

#### 任务组 D：`im-` IM 深化（M30，独立）✅（M30，69aa934）
- **设计依据**：unfinished.md §二十五 + docs/10 遗留（卡片消息、应用级 API、事件重试队列）。
- **现状**：飞书/钉钉/企微 webhook 回调 + secret Fernet（`runtime/im.py` + `api/v1/im.py`）；缺卡片消息、应用级发消息 API、回调事件重试队列；真实凭证联调未做。
- **完成描述（M30）**：① 出站卡片 `runtime/im_outbound.py`（平台无关卡片 → 飞书应用级 im/v1/messages / 钉钉机器人 actionCard / 企微 template_card 三适配器，按钮 value/actionURL/key 编码 Action 协议回调引用，HTTP 发送可注入）；② 应用级凭据 `app_id`/`app_secret_enc`（Fernet 加密，M26 同款；`/channels` 创建 + `/channels/{name}/credentials` 端点，响应不回显）；③ 投递重试队列 `IMOutboundLogRecord`（幂等键 channel+event_key 唯一，direction in|out；进程内 asyncio 循环指数退避 `EAP_IM_RETRY_*` 可配，超限 dead；入站回调业务失败自动入队重投）；④ `POST /channels/{id}/send-card`（admin+审计 `im.send_card`，event_key 幂等）+ `tests/test_im_outbound.py` 13 用例离线绿。迁移 `b4c6d8e0f2a4`（接 a3e5f7b9c1d2）。
- **剩余工作**：真实凭证联调（→ L3 遗留）；通讯录/群管理应用 API 未含。
- **DoD**：三平台卡片下发 mock 测试绿；回调重复投递幂等；凭据加密存储。✅
- **依赖**：无。

#### 任务组 E：`a2a-` A2A 深化 + Agent Discovery（M30，独立）✅（M30，bff9797）
- **设计依据**：unfinished.md §三十五 + docs/04 §5（A2A 1.0 Task 生命周期含流式与异步任务）。
- **现状**：`POST /a2a/rpc`（JSON-RPC message/send）+ `/.well-known/agent-card.json` 单 agent 名片（`api/v1/a2a.py` 122 行）；`a2a.py:6` 自述「流式/推送通知暂不支持」；**平台作为 A2A Client 委派外部 Agent 未做**；平台级 discovery 目录未做。
- **剩余工作**：① `message/stream`（SSE）+ `tasks/pushNotificationConfig` 推送通知；② A2A Client：平台 Agent 委派外部 Agent（委派边界并入 agent-allowlist 策略，复用 M24 delegate 机制）；③ 平台级 discovery 端点（列出全部可发现 agent 的目录）；④ 跨租户委派默认拒绝 + 策略放行。→ ①②③④ 已完成（`runtime/a2a_client.py` + `api/v1/a2a.py` + `tests/test_a2a_v2.py`，15 passed；推送回执覆盖 RPC 内同步完成场景，异步任务引擎完成点未挂钩，见模块 docstring）。
- **DoD**：外部 A2A 客户端流式调用平台 agent；平台 agent 经 delegate 调外部 mock A2A server 全链路测试绿。
- **依赖**：无。

#### 任务组 F：`gateway-` API Gateway（M31，独立）
- **设计依据**：unfinished.md §三十四（Rate Limit/Concurrency/Timeout/Circuit Breaker/Idempotency/Request Size）。
- **现状**：按凭证限流已有（`EAP_CHAT_RATE_LIMIT`，429+Retry-After，覆盖 /v1/chat 与 invocations，M26）；中间件仅有 CORS/Trace/MCPAuth（`main.py`）；**缺并发上限、请求体上限、熔断、幂等键**。
- **剩余工作**：① 全局中间件层：按租户/凭证并发上限、请求体大小上限、全局超时；② 熔断（按 provider/model 5xx 率跳闸，与模型降级链联动）；③ 写操作幂等键（`Idempotency-Key` 头，Redis 可选）；④ 网关指标透出（/metrics）。
- **DoD**：并发超限 429、熔断跳闸与半开恢复、幂等重放不重复执行，均有测试覆盖。
- **依赖**：无。

### 次线：v0.9 生产化（优先级 2，v0.8 主体后开工）

#### 任务组 P1：`prod-worker-` 分布式 Worker / 队列深化
- **现状**：AsyncioQueueBackend + 可选 Redis Streams（`EAP_REDIS_URL`，`runtime/tasks.py`）+ 多副本调度锁（multireplica 测试）+ PENDING 崩溃恢复。
- **剩余工作**：多 worker 抢占语义强化（可见性超时/租约）、任务幂等键、队列优先级、独立 worker 进程模式（`python -m eap.worker`）。
- **DoD**：双进程并发消费不重复、不丢失（压力测试）。

#### 任务组 P2：`prod-sandbox-` 工具/代码沙箱
- **现状**：无沙箱（扩展=受信代码，进程内执行，docs/04 §8 既有约定）。
- **剩余工作**：高风险工具/技能脚本的受限执行（子进程 + 超时 + 资源限制 + 文件系统隔离），经策略 risk_level 联动。
- **DoD**：超时/越界脚本被隔离拒绝且有测试。

#### 任务组 P3：`prod-env-` 环境体系 + Workflow 版本化
- **现状**：Agent 配置版本层已有（M18）；Prompt 有版本+回滚；**Workflow 无版本、无环境标签**。
- **剩余工作**：Workflow 版本（draft→publish→rollback，对齐 Agent 模式）；DEV/TEST/STAGING/PROD 环境标签与发布晋升；与 canary 联动。
- **DoD**：Workflow 发布/回滚/环境隔离测试绿；画布可切环境。

#### 任务组 P4：`prod-ha-` HA / 备份 / 恢复 / DR
- **现状**：备份 cron 化（M15.5/M13.3）+ 生产 runbook（docs/11）。
- **剩余工作**：恢复演练自动化脚本、双实例 HA 部署样例（compose/k8s）、健康告警对接。
- **DoD**：一键恢复演练脚本在干净环境跑通。

#### 任务组 P5：`ci-` CI/CD 强化
- **现状**：GitHub Actions 全量回归 + Docker 镜像 job + Playwright E2E（M17.5/M15.6）。
- **剩余工作**：发布流水线（tag → 镜像推送 → 环境晋升）、文档/契约一致性检查 job。
- **DoD**：打 tag 自动发布镜像并可晋升环境。

### 收尾：v1.0 整合验收（优先级 3）

#### 任务组 V：`v1.0-` 全域整合验收
- **剩余工作**：对照 unfinished.md §四十九 能力地图逐域核对；README/docs 全量同步；生产部署演练；收版审计（docs/16）；v1.0 版本发布。
- **依赖**：主线 A~F 与 P 组完成。

### 长期悬置（需外部条件或产品决策，按条件逐项消化）

| 编号 | 项 | 阻塞原因 / 说明 |
|---|---|---|
| L1 | vLLM multi-LoRA 托管 + 评测门禁接入模型路由 | 需 GPU 环境（docs/10 遗留） |
| L2 | Harness 桌面端（Tauri，订阅同步、沙箱） | docs/09 M3 验收项，至今零进展，需产品决策 |
| L3 | IM 真实凭证联调（飞书/钉钉/企微生产账号） | 需外部账号；代码侧由任务组 D 交付 |
| L4 | SLO 99.9% 验收 + DR 实战演练 | 需生产环境；依赖 P4 |
| L5 | 技能市场分发站点 + 技能包 scripts/assets 附件 | 产品级需求，待排期 |
| L6 | Permission-aware RAG 文档级 ACL | 租户过滤已做（M23）；缺文档/角色级 ACL，需权限模型设计 |
| L7 | Memory 深化：LLM 摘要压缩、agent/组织层 scope、importance 权重、组织记忆联动知识中心 | 当前为 session/user 两层 + 字符截断压缩 |
| L8 | MCP 深化：OAuth（复用 OIDC 客户端）、长连接复用 | docs/10 遗留 |
| L9 | 多租户计费细化（单价账单、配额分层） | 成本报表已有（M26），计费产品化待决策 |
| L10 | 评测深化：人工抽检、在线影子流量、A/B 效果报表回流 | Prompt A/B 已有，转化指标回流未做 |
| L11 | A2A 跨租户委派 | 若任务组 E 的 ④ 已覆盖则关闭本项 |

---

## 五、并行批次建议

| 批次 | 并行任务组 | 说明 |
|---|---|---|
| **批次 1 ✅** | A `event-` ＋ D `im-` ＋ E `a2a-` | 已完成（04cabca / 69aa934 / bff9797），全量回归 234 passed |
| **批次 2（当前）** | B `webhook-` ＋ C `connector-` ＋ F `gateway-` | B 依赖的 A 已完成；C 仅 Trigger 部分依赖 A（已就绪）；F 独立——三线可并行开工 |
| 批次 3 | P1＋P3＋P5（或按需 2~3 线） | v0.9 生产化，P2/P4 随后 |
| 批次 4 | V `v1.0-` 收尾 ＋ L 组按外部条件逐项 | 整合验收与悬置项消化 |

> **取任务规则**：每轮从当前批次取一条线，按组内「剩余工作」序号顺序实施；完成即回写本文件（状态 ✅ + commit 号），再取下一项。

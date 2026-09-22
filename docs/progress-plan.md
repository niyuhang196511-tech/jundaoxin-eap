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
| 平台版本 | v1.0.0（已发布，tag v1.0.0）+ M36 批次 7（L6 RAG 文档级 ACL，见下） |
| 最新里程碑 | M38（harness-M38 数据私有化） |
| 代码 commit | 998da0f |
| 快照日期 | 2026-09-21 |
| 测试基线 | **342 passed, 8 skipped, 0 errors**（92s，平台侧）——Harness M37/M38 以 tauri build + 前端 typecheck/vitest 为验收（本地仓 Rust 单元随 cargo check） |
| 下一主线 | **M39 本地自定义技能 + 移动远程审批**（签名包安装/四域权限提示/沙箱执行 + HITL→IM 卡片，docs/18）→ M40 远程触发/移动监控/GA；组织记忆联动 KB（小批次）可插入；L 组其余按外部条件消化（vLLM/IM 联调/SLO/计费） |
| 审计状态 | docs/12（v0.5）/ docs/13（v0.6）/ docs/15（v0.7）/ **docs/16（v0.9.0，方法=逐线审查+继承判定+自动化验证，建议补跑 seal）**；遗留 5 个 MinerU SSRF 维持「补偿控制在位、可接受」判定 |

---

## 一、版本路线总览（unfinished.md §五十）

| 版本 | 主题 | 状态 | 里程碑 | 收版审计 |
|---|---|---|---|---|
| v0.5 | Agent Application（版本层/结构化输出/交互引擎/Action/Artifact） | ✅ 完成 | M18–M22 | docs/12 |
| v0.6 | Platform Governance（工具治理/Policy/评测/成本/Memory 治理） | ✅ 完成 | M23–M26 | docs/13 |
| v0.7 | Extension Platform（统一 Manifest/注册表/Bundle/SDK 契约） | ✅ 完成 | M27–M29 | docs/15 |
| **v0.8** | **Enterprise Integration（Connector/Webhook/Event/IM/A2A/Discovery/Gateway）** | ✅ 完成（七项全部落地：M30 批次 1 + M31 批次 2；真实凭证/外网联调遗留 → L3/L4） | M30–M31 | 待收版审计 docs/16 |
| v0.9 | Production（分布式 Worker/Sandbox/HA/环境体系/CI-CD） | ✅ 完成（P1 Worker/P3 环境体系/P5 CI-CD/P2 沙箱/P4 HA-DR 全部落地） | M32–M33 | docs/16 ✅ |
| **v1.0** | **Enterprise Agent Platform** | ✅ **v1.0.0 已发布**（2026-09-21，tag v1.0.0；14 能力域全 ✅；seal 门禁决策=接受 docs/16 替代，见 docs/16 §三） | M34–M35 + 收版 | docs/16 |

---

## 二、能力域总览（unfinished.md §一 ①~⑭）

| # | 能力域 | 状态 | 完成描述 / 缺口指向 |
|---|---|---|---|
| ① | Identity / Access | ✅ | JWT/OIDC/SSO/API Key 哈希/PostgreSQL RLS/RBAC 收口（`api/security.py`、`runtime/oidc.py`、`observability/rls.py`） |
| ② | Agent Center | ✅ | 配置版本层 draft→publish→archived + rollback + diff + 前端版本抽屉（M18，`runtime/agent_config.py`） |
| ③ | Agent Runtime | ✅ | Loop+HITL 审批+任务恢复+多智能体委派边界+交互挂起+结构化输出（`runtime/loop.py`、`tasks.py`、`multi_agent.py`、`interaction.py`） |
| ④ | Model Center | ✅ | 能力路由+降级链+真流式+定价计量+Mock 确定性合成（`modelhub/`）；vLLM multi-LoRA 悬置 → L1 |
| ⑤ | Knowledge / RAG | ✅ | 三路混合检索+Citation+可插拔组件+RAG 指标评测+MinerU/表格解析；文档级 ACL → L6 |
| ⑥ | Workflow | ✅ | DSL v2 图执行+拖拽画布+interaction 节点+变量快照挂起续跑（`runtime/workflow.py`）+ 版本化/四环境/回滚/画布环境切换（M32，`runtime/workflow_versions.py`） |
| ⑦ | Tool / MCP / Plugin | ✅ | Tool Governance（M24）+MCP Client/Server+插件热载+Extension Platform（M27–29） |
| ⑧ | Connector | ✅ | 端点工具化+风险映射+secret 加密 + SQL 只读连接器/OAuth2 凭证托管/Health Check/Trigger 联动（M31，`runtime/connectors.py`）；真实 IdP/PostgreSQL 运行时联调 → L3 |
| ⑨ | Memory | ✅ | 四层 scope（session/user/agent/org）+importance 加权召回+TTL+LLM 摘要压缩+租户过滤/保留期/批量遗忘导出（M26+M34，`runtime/memory.py`）；组织记忆联动知识中心 → L7 剩余 |
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

#### 任务组 B：`webhook-` 对外 Webhook 推送（M31）✅（M31，8289327）
- **设计依据**：unfinished.md §三十五（接入方式：Webhook 出站）。
- **现状**：无出站 webhook；事件总线未建。
- **完成描述（M31，待提交）**：① 端点模型 `WebhookEndpointRecord`（URL / secret Fernet 加密 / events JSON 订阅 pattern 列表，fnmatch 通配）+ 投递记录 `WebhookDeliveryRecord`（payload/attempts/next_retry_at/status pending|done|dead/response_status），迁移 `c6f0a2b4d8e1`（接 b4c6d8e0f2a4）；② 推送引擎 `runtime/webhooks.py`（lifespan 挂载照 triggers.py：总线全量订阅 → 端点 pattern 匹配 → payload 事件五元组原样 + meta → X-EAP-Signature = HMAC-SHA256(raw_body, secret) 与 M30 入站对称，附 Event-Id/Type/Timestamp 头 → HTTP POST 10s 超时；进程内 asyncio 重试循环照 im_outbound：指数退避 base*2^(n-1) 封顶 300s，`EAP_WEBHOOK_*` 可配，超限 dead 死信；HTTP 发送 `send_webhook` 可注入；每次投递审计 webhook.deliver）；③ API `api/v1/webhooks.py`（整路由 admin：端点 CRUD + 审计 webhook.create/update/delete，URL http(s)/pattern/重名校验，secret 不回显；`GET /deliveries` 按 endpoint/status 过滤 + limit/offset 分页；`POST /deliveries/{id}/redeliver` 死信/在途手工重投 attempts 重置 + 审计；`POST /{id}/test` 样例事件走真实投递通道）；④ 控制台 `Integrations.tsx` 增 Webhooks Tab（端点列表/创建编辑弹窗 secret 不回显/投递记录子列表失败标红/死信重投/试投按钮）+ `lib/api.ts` webhooksApi；⑤ `tests/test_webhooks.py` 11 用例离线绿（签名重算、事件→签名推送 done、失败→重试→成功、超限→dead、deliveries 过滤分页、手工重投、CRUD 非 admin 403、不匹配不投递、试投、加密落库）。
- **剩余工作**：① WebhookEndpoint 模型 + 管理 API（URL/secret/事件订阅列表，admin+审计）；② 事件 → HMAC-SHA256 签名推送 → 指数退避重试（复用任务引擎）→ 死信可见；③ 控制台配置页。
- **DoD**：订阅 `agent.run.completed` 后本地接收端收到带签名 payload；断连重试与死信可查询。
- **依赖**：A（事件总线）。

#### 任务组 C：`connector-` 连接器深化（M31）✅（M31，0fe5328）
- **设计依据**：unfinished.md §二十五 Connector Center（Authentication/Credential/Action/Trigger/Webhook/Health）。
- **现状**：ConnectorRecord + 端点工具化（`runtime/connectors.py`）+ requires_approval→risk=high（M24）+ secret Fernet（M26）；**缺 SQL 连接器、OAuth 凭证托管、Health Check、Trigger 联动**。
- **完成描述（M31）**：① SQL 连接器 kind `register_connector_kind("sql", …)`（endpoints `{name, query, params}`，config `{dialect, database}` 存新列 `connectors.config`；只读白名单——剥注释后首词须 SELECT + 写关键字词边界拒绝（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/DETACH/PRAGMA/VACUUM/GRANT/REVOKE）+ 拒分号多语句；命名占位 `:name`→`?` 绑定防注入；`asyncio.wait_for` + `to_thread` 超时；行数上限 `EAP_CONNECTOR_SQL_MAX_ROWS` 默认 200 超出截断标 truncated；结果 `{columns, rows, truncated}`；psycopg 缺环境运行时报 EAP-7003 清晰错误，未新增依赖）；② OAuth2 凭证托管（ConnectorRecord 补 `oauth_client_id/client_secret_enc/token_url/access_token_enc/refresh_token_enc/expires_at/scopes` 七列，access/refresh Fernet 加密；client_credentials 工具调用前自动获取 + authorization_code `GET …/oauth/authorize`（一次性 state 防 CSRF）+ `POST …/oauth/callback` + `POST …/oauth/token`（admin 手动获取/刷新，不回显 token）；过期自动 refresh_token 刷新；token HTTP 经可注入 `http_client_factory`（默认 httpx）；REST handler 配置 oauth 即带 `Authorization: Bearer`）；③ Health Check `POST /api/v1/connectors/{name}/health`（admin+审计 `connector.health`：rest GET 5s 2xx=ok / sql SELECT 1 / mock-erp 恒 ok），落库 `last_health_at/last_health_ok` 并在列表 `_view` 透出；④ Trigger 联动：`target_type` 校验增 `connector`，`fire()` 增分支——payload 含 `endpoint`/`arguments` 经 `load_connector_tools` 解析同步执行，审计 trigger.fire（detail 标 connector，ref=结果摘要）；连接器调用路径统一 emit `connector.invoked`（try/except 不阻断）。测试 `tests/test_connector_v2.py` 9 用例离线绿（假 transport 禁真实网络）。迁移 `d7a1b3c5e9f2`（接 c6f0a2b4d8e1）。
- **剩余工作**：psycopg/postgreSQL 运行时未实现（sqlite 首要落地，缺环境报清晰错误）；OAuth authorize 端点从 token_url 推导（`…/token`→`…/authorize`），非该约定 IdP 需显式传 `authorize_url`；多副本部署下 oauth state 为进程内语义。
- **DoD**：sqlite demo 连接器建 Connection → 工具调用 → 审计；OAuth 令牌刷新测试绿。✅
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

#### 任务组 F：`gateway-` API Gateway（M31，独立）✅（M31，fe64dce）
- **设计依据**：unfinished.md §三十四（Rate Limit/Concurrency/Timeout/Circuit Breaker/Idempotency/Request Size）。
- **现状**：按凭证限流已有（`EAP_CHAT_RATE_LIMIT`，429+Retry-After，覆盖 /v1/chat 与 invocations，M26）；中间件仅有 CORS/Trace/MCPAuth（`main.py`）；**缺并发上限、请求体上限、熔断、幂等键**。
- **完成描述（M31）**：① `observability/gateway.py` 三中间件：RequestSizeLimit（Content-Length 超限 413，默认 10MB）、ConcurrencyLimit（按凭证在途上限 429+Retry-After，内置非流式请求超时 504）、Idempotency（/api/v1 写方法 + Idempotency-Key 头 → 同键重放带 X-Idempotent-Replay，进程内存储 + 在途 Future 合并 + TTL 清扫，Redis 可选分支；SSE 路径 /v1/chat/completions 与 /a2a 及 Accept: text/event-stream 前缀排除）；② CircuitBreaker（按模型滑动窗口错误率 → open → 冷却半开单次探测 → 成功 close/失败 re-open，伪时钟可注入）接入 modelhub 路由：open 模型从降级链剔除、成败记录到 breaker，指标 eap_circuit_state（gauge 0/1/2）+ eap_circuit_trips_total；③ 网关指标 eap_gateway_rejected_total{reason} / eap_idempotent_replays_total / eap_gateway_inflight（gauge），metrics.py 新增 gauge_set；④ config.py 追加 EAP_GATEWAY_*（并发默认 0=关、超时默认 0=关、体上限默认 10MB、TTL 300s）；main.py 中间件按 add_middleware LIFO 逆序注册（请求流经 Size→Concurrency→Idempotency）。零 schema 变更（熔断/幂等均进程内存态）。`tests/test_gateway.py` 16 用例离线绿（中间件单测 asyncio.run 驱动 + TestClient 集成 + 伪时钟熔断 + 路由降级链联动）。
- **剩余工作**：全量回归待三线合并后统一跑；Redis 幂等分支仅实现未测（无 Redis 环境）；chunked 无 Content-Length 请求体不拦（生产前置反代收口）。
- **DoD**：并发超限 429、熔断跳闸与半开恢复、幂等重放不重复执行，均有测试覆盖。✅
- **依赖**：无。

### 次线：v0.9 生产化（优先级 2，v0.8 主体后开工）

#### 任务组 P1：`prod-worker-` 分布式 Worker / 队列深化 ✅（M32，d6b03c8）
- **现状**：AsyncioQueueBackend + 可选 Redis Streams（`EAP_REDIS_URL`，`runtime/tasks.py`）+ 多副本调度锁（multireplica 测试）+ PENDING 崩溃恢复。
- **剩余工作**：多 worker 抢占语义强化（可见性超时/租约）、任务幂等键、队列优先级、独立 worker 进程模式（`python -m eap.worker`）。
- **完成描述（M32）**：① 执行租约：TaskRecord 加 `lease_expires_at` 列，worker 取任务时置 now+`EAP_WORKER_LEASE_SECONDS`（默认 300），终态/取消清空；`_recover_leases` 周期扫描（60s + 引擎启动即时）将「RUNNING 且租约过期」重置 PENDING 重跑（抢占式 UPDATE 防多副本重复入队，与 `_recover_pending` 分工：前者管 PENDING 遗留、后者管 RUNNING 泄漏）；引擎 stop 先置停机标记，在途任务回退 PENDING 并清租约（不误判崩溃）。② 任务幂等键：`submit(..., idempotency_key=None)`，同 key 且在途（PENDING/RUNNING/WAITING_*）命中直接返回既有任务；返回值用 str 子类 `TaskSubmitResult`（值即 task_id + `.existing` 属性），既有调用方（kb/evals/triggers）零改动；API 走 body 字段 `idempotency_key`（`Idempotency-Key` 头已被 M31 网关幂等中间件占用，两层互补），响应带 `existing`。③ 队列优先级：TaskRecord 加 `priority`（数值大优先、同级 FIFO）；AsyncioQueueBackend 改 heapq 堆消费 `(-priority, seq, task_id)`；RedisStreamBackend 双流 `eap:tasks:hi`/`eap:tasks`（priority>0 入 hi，lo 复用基础 stream 名保持既有消费端/监控兼容），消费先 hi 短 block 再 lo，XAUTOCLAIM 两流都做且重投保留优先级。④ 独立 Worker：新 `worker.py`（`python -m eap.worker`，不导入 eap.main），init_db → 打版本/配置日志 → TaskEngine（`EAP_WORKER_COUNT` 默认 2）→ SIGINT/SIGTERM 优雅停（drain 停入队等在途 ≤30s → engine.stop）；config.py 加 `worker_count`/`worker_lease_seconds`。迁移 `a9c1e3f5b7d2`（幂等补列 + idempotency_key 索引，接 d7a1b3c5e9f2）。`tests/test_worker.py` 9 用例离线绿（租约恢复/不误恢复/停机回退/drain/幂等引擎级+API 级/优先级出队序/Redis 双流+XAUTOCLAIM 按 skip 机制/入口冒烟断言不导入 eap.main）。
- **剩余工作**：双进程并发消费压力测试与 Redis 分支联调需真实 Redis 环境（本地不可达，Redis 用例自动 skip；test_tasks_redis 存量用例与双流兼容——priority=0 仍走基础 stream）；多副本同 key 并发提交存在非原子窗口（无部分唯一索引，at-least-once 语义可接受）。
- **DoD**：双进程并发消费不重复、不丢失（压力测试）。租约恢复/幂等/优先级/优雅停已有测试覆盖 ✅

#### 任务组 P2：`prod-sandbox-` 工具/代码沙箱 ✅（M33，94dc33b）
- **现状**：无沙箱（扩展=受信代码，进程内执行，docs/04 §8 既有约定）。
- **完成描述（M33，待提交）**：① 新模块 `runtime/sandbox.py`：`SandboxRunner` 子进程受限执行引擎（`sys.executable script_path`，单行 JSON stdin 进/stdout 出协议；超时先 terminate 宽限 3s 再 kill——POSIX `start_new_session`+`os.killpg` 进程组树杀、Windows proc.terminate/kill；stdout/stderr 各 256KB 截断带标记；异常/超时一律返回结构化结果 `{ok, exit_code, stdout, stderr, duration_ms, timed_out, limits_applied, truncated, sandbox_dir}` 不上抛；`run_async` 经 `asyncio.to_thread` 线程池承载防阻塞事件循环，模块级默认实例复用）。资源限制：POSIX `preexec_fn` 设 RLIMIT_CPU/RLIMIT_AS；**Windows 降级语义**（resource 为 POSIX 专属）仅强制超时+FS 隔离+env 裁剪，`limits_applied` 如实反映实际生效项。文件系统隔离为**软隔离：约定+环境裁剪，非容器级**（cwd=一次性临时目录沙箱根，相对写入随执行销毁；不 mounts 不 chroot，绝对路径越界写入无技术阻断，诚实标注于 docstring）。环境裁剪：仅透传 PATH/TEMP/TMP/SYSTEMROOT/LANG 白名单（另注入 PYTHONIOENCODING/PYTHONUTF8 两个协议变量强制子进程 UTF-8，防中文 Windows locale 下 JSON 错乱）。② 脚本工具类型：`Tool` 加 `runtime`（inproc|script，默认 inproc 存量零影响）与 `script_path` 字段；`script_tool()` 工厂（handler 经 SandboxRunner 喂 args JSON/回注结果 JSON，失败回注 `{"error": ...}` 与现有工具失败语义一致）+ `run_script_in_sandbox()` 统一执行入口（工厂 handler 与 loop 路由共用，防绕过）；demo 脚本 `examples/sandbox/echo.py`（回显+pwd+环境键清单）。③ 策略联动：新 kind `tool-sandbox`（config `{"mode": "enforce"|"audit", "tools": [...]}`，policies API 白名单+校验），`policy.check_tool_sandbox()` 返回执行决策（script 工具命中清单必须走沙箱防绕过；inproc 工具 enforce→PolicyDenied「被要求沙箱执行但为进程内实现」/audit→放行+violation）；挂进 loop.py 工具执行块（M24 拦截链中、审批门之后），enforce 拒绝与 audit 放行均落 `tool.sandbox.violation` 审计 + `eap_sandbox_violations_total{mode}`；扩展中心工具目录透出 `runtime` 字段。④ 指标：`eap_sandbox_exec_total{result=ok|timeout|error}` + `eap_sandbox_violations_total{mode=enforce|audit}`。零 schema 变更（策略 kinds 用 PolicyRecord 现有表，脚本工具不落新表）。`tests/test_sandbox.py` 11 用例离线绿（echo 往返/超时杀进程/FS 约定隔离与 cwd 断言/env 裁剪/mem 限额 skipif win32/策略 API 校验/决策矩阵/script_tool 全链路/失败回注/loop 沙箱路由防绕过毒丸验证/enforce 拒绝 inproc+violation 审计），test_tool_governance 等存量回归绿。
- **剩余工作**：软隔离语义边界如实记录——绝对路径越界写入无技术阻断（非容器级，需容器/bwrap 才能升级），越界「拒绝」体现为沙箱根一次性销毁 + 策略审计可观测，FS 隔离测试为约定检查（脚本 os.getcwd()==沙箱根）而非权限断言；Windows 无 rlimit（POSIX 限额用例 skipif win32，跨平台仅超时/FS 约定/env 裁剪可跑）；沙箱执行占 asyncio 默认线程池，大量长时限脚本并发需池容量评估；多副本幂等无涉（每次执行独立临时目录、无共享态）；preexec_fn 为 POSIX fork+exec 传统方案，多线程父进程存在文档化限制。
- **DoD**：超时/越界脚本被隔离拒绝且有测试。✅

#### 任务组 P3：`prod-env-` 环境体系 + Workflow 版本化 ✅（M32，22d7a24）
- **现状**：Agent 配置版本层已有（M18）；Prompt 有版本+回滚；**Workflow 无版本、无环境标签**。
- **完成描述（M32）**：① 模型/迁移：新表 `workflow_versions`（`WorkflowVersionRecord`：workflow_id FK、version 整数递增、dsl JSON 快照、env 可空标签 dev|test|staging|prod、state draft|published|archived、note、published_at，唯一约束 (workflow_id, version)）；`WorkflowRecord` 加 `published_version_id` 生产指针列（按 channel_id 惯例用普通 Integer 不加 DB 级 FK——与 workflow_versions 双向引用成环，SQLite 无法 ALTER 加约束）；迁移 `b8d2f4a6c0e3`（幂等建表+加列，接 P1 线 `a9c1e3f5b7d2`）。② 版本服务 `runtime/workflow_versions.py`：save_draft（max+1 递增）/publish（复用 WorkflowSpec 解析做发布期 DSL 校验，同 env 旧 published 自动 archived，env=prod 同步指针，单 env 绑定语义：跨环境发布=迁移、降级清悬空指针）/rollback（该 env 最近 archived 重发布，prod 同步指针）/diff_dsl（按节点/边 id 结构化差异，形态对齐 agent diff）/resolve_dsl 执行解析链（显式 version > env 指定 > prod 指针 > WorkflowRecord.dsl 兜底——无版本记录时行为不变，存量零影响）。③ 桥接 `workflows.py`：get_spec/load_enabled 走解析链；test_run_async 支持 env/version 覆盖（不落版本）；`refresh_registration` 发布/回滚后按解析链重建 spec → 同模块重注册=替换 → 重启（workflow-as-agent 热更新，语义同 agents 版本发布生效）。④ API `api/v1/workflows.py`：GET/POST `/{name}/versions`、GET `/{name}/versions/{id}`（DSL 全文，画布预览数据源）、POST `/{name}/versions/{id}/publish`、POST `/{name}/versions/{id}/rollback`（写操作 admin+审计 `workflow.version.*`，风格对齐 agents versions）、GET `/{name}/versions/{a}/diff/{b}`、test-run body 增 env/version。⑤ 前端：画布左栏「版本」按钮 + `canvas/VersionDrawer.tsx`（版本列表 + 发布到环境 + 回滚 + 存草稿，精简自 agents VersionDrawer，diff 入口后置）；画布顶部环境选择器（草稿/dev/test/staging/prod）——选环境加载该 env 发布版 DSL 只读预览（未发布提示回草稿，预览中禁用保存）；`lib/api.ts` 增 `workflowVersionsApi`。⑥ 测试 `tests/test_workflow_versions.py` 9 例：草稿递增、dev 发布旧版归档/prod 同步指针、坏 DSL 挡发布、rollback 恢复上一版+指针同步、diff、解析链全序（显式 version > env > 指针 > 兜底，两个内容不同版本以标记工具输出断言）、存量无版本工作流行为不变、RBAC member 403、publish/rollback 后 workflow-as-agent 注册 spec 即时变化。零新增依赖。
- **剩余工作**：画布版本 diff 可视化入口后置；与 canary 联动（P2/发布治理）未做；alembic 升级链依赖 P1 线 `a9c1e3f5b7d2` 先合入。
- **DoD**：Workflow 发布/回滚/环境隔离测试绿；画布可切环境。✅

#### 任务组 P4：`prod-ha-` HA / 备份 / 恢复 / DR ✅（M33，a2928f8）
- **现状**：备份 cron 化（M15.5/M13.3）+ 生产 runbook（docs/11）。
- **完成描述（M33，待提交）**：① 一键备份/恢复演练脚本 `scripts/drill_restore.py`（纯标准库，零新增依赖）：backup 子命令按 EAP_DB_URL 形态自动选择——SQLite 走 sqlite3 在线备份 API（一致性快照，不中断写入）/ PostgreSQL 调 pg_dump `-Fc` Custom 格式（SQLAlchemy 连接串自动转 libpq 形态）；drill 子命令（核心 DoD）`--backup`/`--latest` 定位备份 → 临时目录还原（SQLite 直接副本 + 只读预检，坏文件在此得到清晰报错；PostgreSQL pg_restore `--clean --if-exists` 到 `--restore-db` 演练库）→ 独立进程 Alembic `upgrade head` 确认可迁移（当前解释器无 alembic 时回退 `uv run --project eap`）→ 冒烟断言（integrity_check + tenants/agents/tasks 关键表可计数 ≥0 + alembic_version 单头）→ 结构化报告（每步 `[步骤 N]` 行/耗时/结论，`--report-json` 机器可读）→ 清理（`--keep` 或失败时保留现场）；退出码全绿 0 / 失败 1，演练全程只读源库。真实跑通：开发库 eap.db backup（1.1MB）→ drill 5/5 步全绿退出码 0（关键表 tenants=1/agents=4/tasks=0，alembic 单头 b8d2f4a6c0e3），负路径（随机字节伪备份）在还原步报「不是有效的 SQLite 数据库」退出码 1。② 双实例 HA 部署样例 `deploy/`：docker-compose.ha.yml（2×无状态 API 实例 EAP_WORKER_COUNT=0 + 独立 worker `python -m eap.worker` + PostgreSQL/Redis healthcheck + depends_on 启动顺序 + YAML 锚点合并共享环境，镜像经 EAP_IMAGE 注入 ghcr 晋升标签）、nginx.conf（upstream least_conn + max_fails 自动摘除、/health 直通、SSE proxy_buffering off + 3600s 长超时、client_max_body_size 对齐网关 10MB 上限）、k8s-ha.yaml（Secret 占位 + Deployment 2 副本 + Service + /health readiness/livenessProbe + 独立 worker Deployment + resources 建议，样例级并注明生产需补 Ingress TLS/HPA/PDB 与外置 PG/Redis）、prometheus-alerts.yml（8 条规则全部基于 /metrics 实际导出指标名：实例宕机 sum(up{job="eap"})<2 与全宕、5xx 占比>5%、平均延迟、任务失败增速、熔断 eap_circuit_state、Agent 错误、网关在途）。③ runbook 增补 §8「恢复演练与 HA 部署」（drill 用法 + 月度 crontab 演练建议 + compose.ha 启动顺序 + 晋升切换要点 + 告警接入）。验证：三份 YAML pyyaml 解析 + 结构断言通过；check_docs.py 全绿（新增 5 链接有效）。
- **剩余工作**：pg_dump/pg_restore/psycopg 分支无环境未实测（按 runbook §3/§4 既有规程书写，仅 --help 与代码审查交付）；k8s 样例为样例级（Ingress TLS/HPA/PDB/真实 Secret 托管待各环境补齐）；main.py lifespan 固定 task_engine.start(workers=2)，EAP_WORKER_COUNT=0 在 API 实例完全生效需 eap/src 一行改动（改读 settings.worker_count，属并行沙箱线文件域，样例按目标状态书写）；p95 延迟告警与任务积压深度告警分别需 metrics 增加 histogram 桶 / 队列深度 gauge 导出后补充（当前延迟为均值代理，积压无对应指标）。
- **DoD**：一键恢复演练脚本在干净环境跑通。✅

#### 任务组 P5：`ci-` CI/CD 强化 ✅（M32，e49cff6）
- **现状**：GitHub Actions 全量回归 + Docker 镜像 job + Playwright E2E（M17.5/M15.6）。
- **完成描述（M32）**：① 发布流水线 `.github/workflows/release.yml`：push `v*` tag → `release-image` job 构建 `eap/Dockerfile` 推送 `ghcr.io/<owner>/<repo>/eap:<semver>`（去 v 前缀）与 `:latest`，并生成 Release notes（softprops/action-gh-release）；`promote` job（workflow_dispatch，inputs: image_tag/environment）pull→tag→push 重打 `:<environment>` 环境标签，prod 绑定 GitHub environment `production`（审批保护可后配），inputs 白名单校验防注入，ghcr 镜像名全小写规范化；两 job 均 `packages: write, contents: write` + 最小化注释（顶层默认 contents: read）。② 文档/契约一致性检查 `scripts/check_docs.py`（纯标准库，仓库根 `uv run --no-project python scripts/check_docs.py`）：README+docs 相对链接与裸 `docs/...` 引用存在性（锚点/外链/代码块忽略，`docs/16` 前向引用豁免）、全部 mermaid 代码块含 diagrams/*.mmd 轻量校验（声明行/边两侧非空/括号平衡，erDiagram 鸦爪记号不误报）、progress-plan 快照头「代码 commit」git rev-parse + cat-file -e 校验；当前全绿（18 文档 / 62 链接 / 32 裸引用 / 33 mermaid 块），无存量坏链接。③ ci.yml 增独立 job `docs-check`（fetch-depth: 0 + uv setup + workflow YAML pyyaml 解析兜底 + 跑脚本），不影响现有 job。④ docs/11 §7「发布与晋升」+ README runbook 行同步。⑤ 顺手修复存量 bug：ci.yml `Lint (ruff: F/E9 only)` 未加引号，自 M13.1（ef26049）起整个 workflow YAML 解析失败（Actions 会拒绝加载），加引号修复。零后端 schema / runtime 改动。
- **剩余工作**：release.yml 真实运行验证需推送后在 GitHub 观察（tag 触发发布 + promote 两次 dispatch，本地仅 YAML 解析与结构复查）；console 镜像（eap/frontend）自动发布未纳入（CI 仅构建校验）；GitHub Environments 的 production 保护规则需在仓库设置中后配。
- **DoD**：打 tag 自动发布镜像并可晋升环境。✅

### 收尾：v1.0 整合验收（优先级 3）

#### 任务组 V：`v1.0-` 全域整合验收 🔶（收版批次 5 完成，v1.0 正式发布待 L 组消化）
- **完成描述（收版批次 5）**：① 能力域总览终核——14 域全部 ✅（对照 unfinished.md §四十九 能力地图）；② 版本提升 v0.9.0（pyproject + `__init__`）+ README 根目录/eap 双侧特性同步（v0.8/v0.9 增量 + eap/README 过期「已实现 vs 待实现」表替换为账本指引）；③ 收版审计结论 docs/16（方法如实声明：逐线审查+继承判定+自动化验证，建议补跑 seal）；④ 文档一致性检查绿（19 文档/67 链接）；⑤ 全量回归 298 passed 复验版本提升。
- **剩余工作**：v1.0 正式发布前置——seal 深度扫描补跑（docs/16 声明）+ 测试状态泄漏修复（test_tool_governance 租户策略持久，docs/16 §三）+ L 组清零或产品决策。
- **依赖**：主线 A~F 与 P 组完成（均已 ✅）。

### 长期悬置（需外部条件或产品决策，按条件逐项消化）

| 编号 | 项 | 阻塞原因 / 说明 |
|---|---|---|
| L1 | vLLM multi-LoRA 托管 + 评测门禁接入模型路由 | 需 GPU 环境（docs/10 遗留） |
| L2 | Harness 桌面端（Tauri） | **产品形态已决策（2026-09-21，用户确认）**：订阅+快捷调用壳 + **用户数据私有化**（本地数据不出端）+ **自定义本地技能** + **移动远程操作**（参考 Codex/ZCode/workbuddy：IM 卡片远程审批/远程触发/移动监控，不做原生 App）。设计方案 docs/18 四期分解（M37 壳工程/M38 数据私有化/M39 本地技能+远程审批/M40 远程触发+GA），待批准后实施 |
| L3 | IM 真实凭证联调（飞书/钉钉/企微生产账号） | 需外部账号；代码侧由任务组 D 交付 |
| L4 | SLO 99.9% 验收 + DR 实战演练 | 需生产环境；依赖 P4 |
| L5 | 技能市场分发站点 + 技能包 scripts/assets 附件 | 🔶 附件包工程部分 ✅（M34，d550af3）：bundle 扩展 scripts/（仅 .py，执行走 M33 沙箱 script_tool）与 assets/（白名单扩展），sha256 清单随签名覆盖、安全校验镜像 M28、落盘/清单/下载/导出往返端点、scaffold 模板示例；**分发站点仍待产品决策** |
| L6 | Permission-aware RAG 文档级 ACL | ✅（M36，ce4f97e，设计 docs/17 已批准）：document_acls 表（deny 优先→allow→默认可见，KB 级默认+文档级覆盖）+ 三路检索单点过滤（Reranker 前，计数不泄露） + 图谱概览同步过滤 + 管理 4 端点（admin+审计）+ 前端访问控制弹窗；chunk 级 ACL/group 主体为设计预留 |
| L7 | Memory 深化：LLM 摘要压缩、agent/组织层 scope、importance 权重、组织记忆联动知识中心 | 🔶 工程部分 ✅（M34，49a5ea4 + 6e0710f 接线）：scope 扩为 session\|user\|agent\|org（agent 按 agent 列过滤、org 租户内共享）、importance 0~1 加权召回打分（final = cos + 0.3\*overlap + 0.2\*importance）、ttl_days→expires_at TTL（recall/list 默认过滤过期，purge 一并清理）、`EAP_MEMORY_SUMMARY_COMPRESS` 开关的 LLM 摘要压缩（`runtime/context.py` compress_messages：hub.complete 生成摘要→kind=summary 落库→历史替换，失败回退字符截断）；**已接入生产调用链**——`ctx.run_loop` 新增 session_id 参数（order_agent 先行启用），接线测试覆盖摘要调用/替换/落库与未传对照；迁移 c0d3e5a7f9b4；剩余「组织记忆联动知识中心」 |
| L8 | MCP 深化：OAuth（复用 OIDC 客户端）、长连接复用 | ✅（M35，566fd36）：client_credentials 凭证托管（Fernet 加密缓存 + 30s margin 过期自动重取 + 手动刷新端点，模式同 M31 连接器）+ 认证头三级回退（OAuth bearer → api_key → 无）+ per (url, auth 指纹) 连接池长连接复用（token 刷新自动轮换实例）；validate 端点接入认证；完整 MCP 握手 OAuth 联调属 L3 式外部验证（mock IdP 离线 7 测试绿） |
| L9 | 多租户计费细化（单价账单、配额分层） | 成本报表已有（M26），计费产品化待决策 |
| L10 | 评测深化：人工抽检、在线影子流量、A/B 效果报表回流 | 🔶 A/B 报表回流工程部分 ✅（M34，b403ebd）：变体归因埋点（`runtime/prompts.py` resolve_template 实验命中即写审计 prompt.render[experiment/version_selected/picked/percent_b] + variant_scope contextvar；agents 同步调用完成/失败落 agent.run.completed 审计并携带 prompt_variant，经 trace_id 关联 usage_records）；报表 `GET /api/v1/prompts/experiments/{name}/report`（admin+审计 prompt.ab.report，分 variant 聚合渲染数与分流占比 vs percent_b 偏差、归因调用数/成功率、token/成本、延迟 p50/均值，支持 since_hours 时间窗，attribution 字段诚实标注口径）；结论辅助 `POST .../conclude`（admin+审计 prompt.ab.conclude，winner+reason 禁用实验，promote=true 复用版本流水线发布胜出版本）。**归因口径局限（如实）**：renders 精确；调用指标为近似——仅覆盖同步智能体调用内部渲染了实验 prompt 的调用（SSE 流式不落审计不计入；渲染前失败无变体可归因；一次调用渲染多实验按最近命中归因），无数据维度返回 0/null 不编造。零 schema 变更（指标全走审计日志 JSON 聚合）；迁移链修复：e2f5a7b9c3d6 down 改指 c0d3e5a7f9b4 恢复单头线性。剩余：人工抽检、在线影子流量 |
| L11 | A2A 跨租户委派 | ✅ 已关闭——任务组 E ④ 已覆盖（跨租户委派默认拒绝 + `a2a-delegate-allowlist` 策略放行 + 审计，M30） |

---

## 五、并行批次建议

| 批次 | 并行任务组 | 说明 |
|---|---|---|
| **批次 1 ✅** | A `event-` ＋ D `im-` ＋ E `a2a-` | 已完成（04cabca / 69aa934 / bff9797），全量回归 234 passed |
| **批次 2 ✅** | B `webhook-` ＋ C `connector-` ＋ F `gateway-` | 已完成（8289327 / 0fe5328 / fe64dce），全量回归 270 passed——v0.8 七项收官 |
| **批次 3 ✅** | P1 `prod-worker-` ＋ P3 `prod-env-` ＋ P5 `ci-` | 已完成（d6b03c8 / 22d7a24 / e49cff6），全量回归 288 passed；P5 顺带修复 ci.yml 自 M13.1 起的 YAML 解析错误 |
| **批次 4 ✅** | P2 `prod-sandbox-` ＋ P4 `prod-ha-` | 已完成（94dc33b / a2928f8），全量回归 298 passed——**v0.9 五组全部收官**（main.py worker 数接 EAP_WORKER_COUNT 收尾项一并落地） |
| **批次 5 ✅** | V `v1.0-` 收版整合 | 已完成（v0.9.0 发布 + docs/16 审计 + README 双侧同步 + 回归 298 passed）；v1.0 正式发布剩 seal 补扫与 L 组 |
| **批次 6 ✅** | L 组工程部分：memory（L7）＋ evals A/B 报表（L10）＋ skills 附件包（L5）＋ 测试泄漏/调度抖动修复 | 已完成（49a5ea4 / b403ebd / d550af3 + 498bdfd / 88c14b9），全量回归 329 passed |
| **批次 6.5 ✅** | L7 接线（摘要压缩入 run_loop）＋ console 镜像/K8s 补齐（P5/P4 未尽）＋ L8 MCP OAuth | 已完成（6e0710f / 014c993 / 566fd36），回归 337 passed |
| **收版 ✅** | **v1.0.0 发布**（6186583，tag v1.0.0）+ docs/16 门禁决策（无 seal 环境，docs/16 替代） | **14 能力域收官，v1.0 达成** |
| **批次 7 ✅** | M36 L6 文档级 ACL（ce4f97e）＋ **M37 Harness 壳工程基座**（d68359c/250c4cf） | M36 已批准实施完成；M37：Tauri 2 壳（托盘/Alt+Space 热键/关闭驻留）+ 设备绑定认证 （签发/列表/吊销，4 测试）+ Agent 目录订阅与流式快捷调用 MVP；**tauri build 全链路验证通过**（NSIS 安装包产出；本机 winget 安装 VS Build Tools 后） |
| **批次 8（当前）** | **M38 数据私有化 ✅**（998da0f：本地 SQLite 仓/三档数据模式/隐私清单页）；**M39-A 本地技能运行时 ✅（待提交）**（harness/skills.rs：Ed25519 验签安装+附件 sha256 复核+四域授权弹窗+沙箱执行，跨语言验签夹具锚定 skill_pkg；cargo test 13 用例/typecheck/tauri build 三绿）→ M39 移动远程审批 → M40 远程触发/GA | Harness v1.1 主线（docs/18） |
| 批次 8+ | Harness 桌面端 v1.1 主线（形态已决策：订阅+快捷调用 + 数据私有化 + 本地技能）＋ L 组其余外部条件项 | vLLM（GPU）/ IM 联调（外部账号）/ SLO（生产环境）/ 计费（产品决策）/ 组织记忆联动 KB |

> **取任务规则**：每轮从当前批次取一条线，按组内「剩余工作」序号顺序实施；完成即回写本文件（状态 ✅ + commit 号），再取下一项。

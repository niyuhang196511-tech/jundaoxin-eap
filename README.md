# EAP · 企业级 Agent Operating Platform

> 平台定位：统一管理模型、知识、智能体、工具、技能、Prompt 六类资产，覆盖注册→发布→运行→观测→评测→计费全生命周期，延伸至员工桌面（Harness）与第三方系统（嵌入外链 / A2A）。

## 代码工程（可运行）

[eap/](eap/) —— **v0.7.0：全量测试 200+ 通过**。模型中心（能力路由+降级链+真流式）、知识中心（三路混合检索+Citation+可插拔 RAG 组件）、Agent Runtime（Loop+HITL+任务恢复）、**Workflow 图编排（拖拽画布 + 图执行引擎 + 运行历史）**、扩展开发体系（**手写 Agent/工具/RAG 组件/MCP Server**，插件目录热载 + 脚手架）、多租户（OIDC/SSO + 外部 JWT 资源服务器 + 可选 PostgreSQL RLS）、可观测性（结构化日志 + /metrics + OTel tracing）、运维（Alembic 迁移、API Key 哈希、秘密加密、备份恢复 runbook）。控制台为 Next.js 16 + Tailwind 4 全新前端（Dify 风格画布/对话/扩展中心）。v0.5 新增：**Agent 配置版本层**（draft→publish→rollback 配置覆盖）、**Structured Output**（JSON Schema 约束 + 校验重试 + 前端渲染器）、**交互引擎**（AI UI Schema 9 控件 + 任务/聊天双通道挂起恢复 + 动态选项级联）、**Action**（UI 动作按钮 → 工具统一解析/审批转办/审计）与 **Artifact Center**（产物存储/预览/下载）、**工作流 User Interaction 节点**（变量快照挂起续跑）。v0.6 新增：**Tool Governance**（工具风险分级 + 策略白名单/审批阈值/委派边界，循环内调用审计与指标）、**Policy Engine 扩展**（6 种策略 kinds，任务通道策略上下文补齐）、**评测深化**（LLM 裁判多维评分 + 异步评测 + 回归对比 + RAG 检索指标 HitRate/Recall/MRR/NDCG + 知识库标注即数据集）、**成本计量**（模型定价 + agent 归属 + 成本报表 + SSE 计量归属修复）、**限流多作用域**（按凭证 429+Retry-After）、**Memory 治理**（租户过滤/保留期/批量遗忘/导出）、**审计查询页**（过滤+分页）与 **RBAC 收口**。v0.7 新增：**Extension Platform**——统一 Extension Manifest（8 种扩展类型 + runtime.min_version 兼容检查 + 旧 EAP_PLUGIN 归一化）、持久扩展注册表（启用/停用跨重启保持）、.eapext Bundle 打包/安装/升级/卸载全生命周期（admin + 审计 + 安全校验）、SDK 契约（Tool/RAG/WorkflowNode/Connector/ModelProvider/UI 各类型接入）与 docs/14 开发者指南。

```bash
cd eap
uv sync                 # uv + Python 3.12
uv run pytest           # 全量测试（离线可跑：mock 模型 + hash 嵌入 + SQLite）
uv run python -m eap    # 启动 → http://localhost:8300/docs
cd frontend && pnpm install && pnpm dev   # 控制台 → http://localhost:3000
```

生产部署：[docs/11-production-runbook.md](docs/11-production-runbook.md)。详见 [eap/README.md](eap/README.md)。

## 设计文档

- **想 3 分钟看懂全貌** → 打开 [architecture.html](architecture.html)（交互式架构图：点击模块查看职责，滚轮缩放，拖拽平移）
- **想 10 分钟读懂架构** → [docs/02-总体架构设计.md](docs/02-总体架构设计.md)
- **评审/立项汇报** → architecture.html + docs/01 + docs/09

## 文档目录（建议阅读顺序）

| # | 文档 | 一句话说明 |
|---|---|---|
| 01 | [需求与竞品分析](docs/01-需求与竞品分析.md) | 10 项需求→模块映射、竞品能力矩阵、文献来源 |
| 02 | [总体架构设计](docs/02-总体架构设计.md) | 三面七层、十大中心、七大运行组件、部署概览 |
| 03 | [Agent Runtime 与执行模型](docs/03-AgentRuntime与执行模型.md) | 内核三件套、Context Engineering、长任务、注册钩子与 Manifest |
| 04 | [核心机制设计](docs/04-核心机制设计.md) | 知识/RAG V2、模型中心与专用模型接法、技能、MCP/A2A、外链、Harness |
| 05 | [领域模型与数据模型](docs/05-领域模型与数据模型.md) | ER、核心表、组合版本、数据生命周期 |
| 06 | [安全与治理架构](docs/06-安全与治理架构.md) | 安全六域、信任边界、多租户隔离、版本发布环境治理 |
| 07 | [API / SDK / Protocol 规范](docs/07-API-SDK-Protocol规范.md) | 凭证、对外 API、Manifest Schema、platform-sdk、JS SDK |
| 08 | [观测评测成本与 SLO](docs/08-观测评测成本与SLO.md) | 链路追踪、监控告警、评测门禁、成本中心、SLO/容量 |
| 09 | [技术选型与实施路线](docs/09-技术选型与实施路线.md) | 选型与许可证风险、M1/M2/M3 里程碑、团队分工、风险清单 |
| 11 | [生产部署 Runbook](docs/11-production-runbook.md) | 上线必改清单、迁移流程、备份恢复、健康告警与常见故障，附 tag→镜像→晋升发布流程 |

## 架构图源文件

`diagrams/` 下 17 个 mermaid 源文件（01 总体架构 … 17 发布生命周期），已内嵌到各文档对应章节，也可在支持 mermaid 的工具中独立维护。

## 下一步

> 进度账本：[docs/progress-plan.md](docs/progress-plan.md)（状态 / 剩余工作 / DoD / 并行分组，完成一项回写一项）。

- **当前主线 v0.8 企业集成**：Event 事件中心（底座）、IM 深化、A2A 深化 + Agent Discovery → Webhook 推送、Connector 深化、API Gateway
- **次线 v0.9 生产化**：分布式 Worker、沙箱、环境体系 + Workflow 版本化、HA/DR、CI/CD
- 在会话中直接说"继续实现 XX"即可按账本推进对应任务组

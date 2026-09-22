"""EAP · 企业级 Agent 智能体平台.

三面七层架构实现（v1.0.0）：
- Control Plane：模型中心（Policy Engine）/ 知识中心 / Agent Registry + 配置版本层 / 发布治理 / 成本 / 策略 / 扩展注册表
- Runtime Plane：Agent Loop + 工具治理 + 沙箱 + Context 组装（LLM 摘要压缩）+ 多智能体 + Workflow（版本化/四环境/交互节点）+ Memory（四层 scope/importance/TTL）
- 生态：MCP Client（OAuth）/Server/Registry、企业连接器（SQL/OAuth）、企业 IM（卡片/重试队列）、事件中心 + Webhook 推送、技能包（附件/签名）分发
- 接入：OpenAI 兼容端点、A2A 1.0（流式/委派/发现）、API 网关（并发/幂等/熔断）、嵌入外链、React 控制台
"""

__version__ = "1.0.0"

"""EAP · 企业级 Agent 智能体平台.

三面七层架构的 M1+M2+M3 实现：
- Control Plane：模型中心（Policy Engine）/ 知识中心 / Agent Registry / 发布治理 / 成本 / 策略
- Runtime Plane：Agent Loop + 工具 + Context 组装 + 多智能体 + Workflow（并行/子流程）+ Memory
- 生态：MCP Client/Server/Registry、企业连接器、企业 IM、技能包签名分发
- 接入：OpenAI 兼容端点、A2A 1.0、嵌入外链、React 控制台
"""

__version__ = "0.3.0"

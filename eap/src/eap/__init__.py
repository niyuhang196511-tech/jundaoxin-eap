"""EAP · 企业级 Agent 智能体平台.

三面七层架构的 M1 核心实现：
- Control Plane 精简版：模型中心（能力路由+降级链）、知识中心（多KB+混合检索）、Agent Registry
- Runtime Plane 精简版：Agent Loop + 工具调用 + Context 组装
- 接入：OpenAI 兼容端点、Agent 调用 API、KB 检索 API
"""

__version__ = "0.1.0"

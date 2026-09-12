"""开发种子数据：租户/密钥/模型注册表/示例知识库。幂等。"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import ApiKey, KB, ModelRecord, Tenant


def run(engine) -> None:  # noqa: C901
    s = get_settings()
    with Session(engine) as db:
        tenant = db.scalar(select(Tenant).where(Tenant.name == s.dev_tenant))
        if tenant is None:
            tenant = Tenant(name=s.dev_tenant)
            db.add(tenant)
            db.flush()

        if db.scalar(select(ApiKey).where(ApiKey.key == s.dev_api_key)) is None:
            db.add(ApiKey(key=s.dev_api_key, tenant_id=tenant.id, note="开发默认密钥"))

        # 开发用嵌入外链渠道（demo.html 直接可用；生产经 API 创建并只存 token 哈希）
        from .models import EmbedChannel

        if db.scalar(select(EmbedChannel).where(EmbedChannel.token == "dev-embed-token")) is None:
            db.add(EmbedChannel(
                agent_name="faq-agent", token="dev-embed-token",
                domains=["*"], note="开发演示渠道（域名不限）",
            ))

        # 示例技能（客服服务规范）
        from .models import SkillRecord

        if db.scalar(select(SkillRecord).where(SkillRecord.name == "customer-service")) is None:
            db.add(SkillRecord(
                name="customer-service", version="1.0.0",
                description="官网客服服务规范",
                instructions="1) 回答以感谢用户提问开场；2) 每次回答不超过三句话，并按要求标注 [n] 引用；"
                             "3) 资料中没有的信息不要编造，引导用户联系人工客服或提交工单。",
                permissions=["kb.retrieve"],
            ))

        # 示例 Prompt（客服回答风格模板）
        from .models import PromptRecord

        if db.scalar(select(PromptRecord).where(PromptRecord.name == "faq-answer-style")) is None:
            from .runtime.context import extract_prompt_variables

            template = ("请作为企业官网客服，用不超过三句话回答用户问题，语气友好。"
                        "用户问题：{{question}}。请仅依据给定资料回答并标注 [n] 引用。")
            db.add(PromptRecord(
                name="faq-answer-style", version="1.0.0",
                description="客服回答风格模板（Prompt 中心示例）",
                template=template,
                variables=extract_prompt_variables(template),
            ))

        # 示例评测数据集（faq-agent 冒烟门禁）
        from .models import EvalDatasetRecord

        if db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == "faq-smoke")) is None:
            db.add(EvalDatasetRecord(
                name="faq-smoke", description="faq-agent 冒烟评测（规则裁判）",
                cases=[
                    {"input": "如何创建知识库？", "expected_any": ["知识库", "FAQ"]},
                    {"input": "如何注册手写的智能体？", "expected_any": ["注册", "register_agent"]},
                ],
            ))

        # 模型注册表：mock 永远可用（离线开发/测试），配置了外部供应商则注册并高优先级
        if db.scalar(select(ModelRecord).where(ModelRecord.name == "mock-llm")) is None:
            db.add(ModelRecord(
                name="mock-llm",
                capabilities=["chat", "reasoning", "extraction"],
                provider="mock",
                priority=100,
                notes="内置确定性 mock，离线可用",
            ))
        if s.openai_base_url and s.openai_api_key:
            if db.scalar(select(ModelRecord).where(ModelRecord.name == "external-llm")) is None:
                db.add(ModelRecord(
                    name="external-llm",
                    capabilities=["chat", "reasoning", "vision"],
                    provider="openai_compat",
                    base_url=s.openai_base_url,
                    api_key=s.openai_api_key,
                    remote_model=s.openai_model,
                    priority=10,
                    notes="外部 OpenAI 兼容供应商（策略放行时优先）",
                ))
        db.commit()

        # 示例知识库（幂等：存在即跳过）
        from .knowledge import service as kb_svc
        from .models import KB

        if db.scalar(select(KB).where(KB.name == "product-docs")) is None:
            kb = KB(name="product-docs", title="EAP 产品文档", template="doc")
            db.add(kb)
            db.flush()
            kb_svc.ingest_text(db, kb, "EAP 平台简介", (
                "EAP 是企业级 Agent 智能体平台，统一管理模型、知识、智能体、工具、技能、Prompt 六类资产。\n\n"
                "模型中心按能力路由：chat、reasoning、embedding、rerank、vision、extraction 等能力，"
                "Agent 按能力声明模型而不写死模型名，路由器按时延、成本、质量、数据边界选择。\n\n"
                "知识中心支持多知识库实例与知识图谱，检索管道为查询改写、三路召回（向量+稀疏+图谱）、"
                "融合重排、引用溯源。"
            ), source="seed")
            kb_svc.ingest_text(db, kb, "下单流程说明", (
                "销售下单流程：首先通过库存查询工具确认库存充足，然后在 ERP 中创建订单，"
                "订单创建后系统自动发送确认邮件给客户。\n\n"
                "库存查询工具为 erp.inventory.query，下单工具为 erp.order.create。"
                "下单前必须核对客户编号与产品编号，避免错发。"
            ), source="seed")
            db.commit()
            print("[seed] 已创建示例知识库 product-docs", file=sys.stderr)

        if db.scalar(select(KB).where(KB.name == "website-faq")) is None:
            kb = KB(name="website-faq", title="官网客服 FAQ", template="faq")
            db.add(kb)
            db.flush()
            kb_svc.ingest_faq(db, kb, [
                {"question": "如何创建知识库？",
                 "answer": "在控制台知识中心点击新建知识库，可选择文档库或客服 FAQ 模板，FAQ 模板支持问答对表格批量导入。"},
                {"question": "如何注册手写的智能体？",
                 "answer": "使用 platform-sdk 编写 AgentApp 并通过 @register_agent 装饰器声明 manifest，"
                           "平台启动时自动发现、校验并纳管，生成统一调用接口与嵌入外链。"},
                {"question": "支持哪些模型供应商？",
                 "answer": "模型中心统一接入 OpenAI 兼容供应商（OpenAI、DeepSeek、通义、本地 vLLM 等），"
                           "并支持注册外部微调的定制模型与专用模型。"},
            ])
            db.commit()
            print("[seed] 已创建示例知识库 website-faq", file=sys.stderr)

"""仓库补货智能体（v0.5-④ 交互引擎演示）：动态选项表单 + 确认 + 结构化输出。

演示交互引擎全链路：
- select（data_source=mock-erp inventory.query 动态选项）
- number / text 表单字段
- confirmation（高风险动作确认）
- ctx.interact 抛 InteractionRequested → 聊天通道 result 帧 / 任务通道 WAITING_INPUT
"""

from __future__ import annotations


from ..app import AgentApp
from ..manifest import AgentManifest
from ..sdk import register_agent
from ...schemas import InvokeRequest, InvokeResult

MANIFEST = AgentManifest(
    name="warehouse-agent",
    version="1.0.0",
    description="仓库补货智能体：交互式选择产品与数量（动态选项），生成补货建议",
    models=[{"capability": "chat", "required": True}],
    permissions=["connector.invoke"],
    tools=["mock-erp"],
    domains=["*"],
)

WAREHOUSE_FORM = {
    "type": "form",
    "title": "补货信息",
    "description": "选择产品并填写补货数量",
    "fields": [
        {"type": "select", "id": "product", "label": "选择产品", "required": True,
         "data_source": {"type": "tool", "tool": "erp.inventory.query",
                          "args": {}, "label_field": "product", "value_field": "product"}},
        {"type": "number", "id": "qty", "label": "补货数量", "required": True,
         "placeholder": "10"},
        {"type": "text", "id": "note", "label": "备注（可选）"},
    ],
}

CONFIRM_FORM = {
    "type": "form",
    "title": "确认提交",
    "fields": [
        {"type": "confirmation", "id": "confirmed", "label": "确认生成补货单？",
         "required": True},
    ],
}


@register_agent(MANIFEST, source="builtin")
class WarehouseAgent(AgentApp):
    async def on_invoke(self, request: InvokeRequest) -> InvokeResult:
        # 聊天通道恢复协议：input 为交互提交 JSON 时从提交值继续
        submitted = self.ctx.interaction_values(request)
        product = (submitted or {}).get("product")
        if not product:
            # 首次执行：交互式收集（抛 InteractionRequested → result 帧）
            values = self.ctx.interact(WAREHOUSE_FORM, key="restock",
                                       title="补货信息收集", resume=None)
            product = values.get("product")
            qty = values.get("qty")
        else:
            # 恢复执行：提交值即表单结果
            qty = submitted.get("qty")

        suggestion = {
            "product": product,
            "qty": int(qty) if str(qty or "").isdigit() else 10,
            "status": "SUGGESTED",
        }
        return InvokeResult(
            content=f"补货建议：{product} × {suggestion['qty']}（交互引擎演示）",
            steps=["interaction: warehouse form submitted", "suggest: restock plan generated"],
            data=suggestion,
        )

    async def on_invoke_task(self, request: InvokeRequest, gate=None, resume: dict | None = None) -> InvokeResult:
        """任务通道：ctx.interact 在无 resume 值时挂起 WAITING_INPUT，提交后从交互点继续。"""
        values = self.ctx.interact(WAREHOUSE_FORM, key="restock", title="补货信息收集",
                                   resume=resume)
        product = values.get("product")
        qty = values.get("qty")
        return InvokeResult(
            content=f"补货建议（任务通道）：{product} × {qty}",
            steps=["interaction: WAITING_INPUT resolved"],
            data={"product": product, "qty": qty, "status": "SUGGESTED"},
        )

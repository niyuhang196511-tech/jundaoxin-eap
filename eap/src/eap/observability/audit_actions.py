"""审计动作目录（M50-B2）：管理面写操作审计动作名的文档性枚举。

对齐 runtime/events.EVENT_CATALOG（M49-E1）先例——

- **目录 = 文档 / 提示性质**：供审计查询页动作过滤做 datalist 候选提示
  （GET /api/v1/audit/actions）。过滤端点仍按 action 精确匹配接受**任意字符串**，
  目录不构成白名单校验；新增动作无需先改目录即可落库与查询。
- 目录条目 = 全仓 ``record(...)`` 留痕调用点（grep 核实）的**静态字面量**动作名，
  外加少数经 helper 转发、值为静态字面量的动作（见下 memory.summary_compress*）。
- **动态动作名不在目录内属预期**：如 Harness 上报端点把客户端提供的动作强制补
  ``harness.`` 前缀后落库（``harness.<client-provided>``），这类名字是运行时数据、
  非源码字面量，不可静态穷举，故不列入目录（过滤时仍可精确查询到）。

按域分组（前缀 = 域），组内按动作名字典序；域顺序按字母排列便于查阅。
"""

from __future__ import annotations

# 审计动作目录（扁平 + 域前缀注释）。修改后请同步 tests/test_audit_actions.py
# 的防漂移静态防线（源码字面量 ⊆ 目录，目录 ⊆ 源码引用）。
AUDIT_ACTIONS: tuple[str, ...] = (
    # agent —— 智能体运行 / 版本生命周期（api/v1/agents.py）
    "agent.run.completed",
    "agent.version.create",
    "agent.version.deprecate",
    "agent.version.publish",
    "agent.version.rollback",
    # audit —— 审计自身的导出 / 保留期清理（api/v1/audit.py）
    "audit.export",
    "audit.retention.purge",
    # budget —— 预算设置 / 导出（api/v1/budgets.py）
    "budget.export",
    "budget.set",
    # connector —— 连接器 CRUD / 健康 / OAuth（api/v1/connectors.py）
    "connector.create",
    "connector.health",
    "connector.oauth.authorize",
    "connector.oauth.callback",
    "connector.oauth.token",
    "connector.toggle",
    "connector.update",
    # embed —— 嵌入会话渠道（api/v1/embed.py）
    "embed.create",
    "embed.disable",
    # eval / shadow —— 评测数据集与运行 / 人工复核 / 影子对比（api/v1/evals.py）
    "eval.dataset.create",
    "eval.review.sample",
    "eval.review.submit",
    "eval.run.submit",
    "shadow.create",
    "shadow.delete",
    "shadow.report.judge",
    "shadow.update",
    # extension —— 扩展注册表启用 / 停用（api/v1/extensions.py）
    "extension.disable",
    "extension.enable",
    # harness —— 一体机设备 / 远程审批 / 审计上报（api/v1/auth.py、im.py、audit.py）
    "harness.audit.report",
    "harness.device.register",
    "harness.device.revoke",
    "harness.remote.approve",
    # im —— IM 渠道 / 凭证 / 卡片下发（api/v1/im.py）
    "im.channel.create",
    "im.credentials",
    "im.send_card",
    # interaction —— 交互表单提交 / 动态选项解析（api/v1/agents.py、runtime/interaction.py）
    "interaction.options",
    "interaction.submit",
    # kb —— 知识库访问控制规则（api/v1/kb.py）
    "kb.acl.create",
    "kb.acl.delete",
    # lora —— LoRA 适配器生命周期（api/v1/lora.py）
    "lora.delete",
    "lora.load",
    "lora.register",
    "lora.unload",
    # mcp —— MCP Server 注册 / OAuth / 开关（api/v1/mcp_registry.py）
    "mcp.oauth.refresh",
    "mcp.register",
    "mcp.toggle",
    # memory —— 记忆遗忘 / 清理 / 固化 / 会话摘要压缩（api/v1/memory.py、runtime/context.py）
    # 注：summary_compress* 经 runtime/context._audit_compress(db, action, ...) 转发，
    #     action 由调用方以静态字面量传入（非拼接），故仍是可穷举的确定动作名。
    "memory.consolidate",
    "memory.forget_user",
    "memory.purge",
    "memory.summary_compress",
    "memory.summary_compress_fallback",
    # model —— 模型注册 / 开关 / 评测门禁拦截（api/v1/models.py、modelhub/router.py）
    "model.eval_gate.blocked",
    "model.register",
    "model.toggle",
    # policy —— 治理策略创建 / 开关（api/v1/policies.py）
    "policy.create",
    "policy.toggle",
    # prompt —— 提示词版本 / AB 实验 / 渲染（api/v1/prompts.py、runtime/prompts.py）
    "prompt.ab.conclude",
    "prompt.ab.report",
    "prompt.create",
    "prompt.publish",
    "prompt.render",
    "prompt.rollback",
    # release —— 发布单生命周期（api/v1/releases.py）
    "release.canary",
    "release.create",
    "release.eval",
    "release.promote",
    "release.rollback",
    # skill —— 技能创建 / 资产导入 / 开关（api/v1/skills.py）
    "skill.assets.import",
    "skill.create",
    "skill.toggle",
    # task —— 任务审批 / 交互提交（api/v1/tasks.py）
    "task.approve",
    "task.interact",
    # trigger —— 触发规则 CRUD / 限流 / 触发 / webhook 拒绝（api/v1/triggers.py、runtime/triggers.py）
    "trigger.create",
    "trigger.delete",
    "trigger.fire",
    "trigger.rate_limited",
    "trigger.update",
    "trigger.webhook.rejected",
    # webhook —— Webhook 端点 CRUD / 重投 / 试投 / 投递（api/v1/webhooks.py、runtime/webhooks.py）
    "webhook.create",
    "webhook.delete",
    "webhook.deliver",
    "webhook.redeliver",
    "webhook.test",
    "webhook.update",
    # workflow —— 工作流 CRUD / 版本 / 重载 / 停用（api/v1/workflows.py）
    "workflow.create",
    "workflow.disable",
    "workflow.reload",
    "workflow.version.create",
    "workflow.version.publish",
    "workflow.version.rollback",
)


def group_by_domain(actions: tuple[str, ...] = AUDIT_ACTIONS) -> dict[str, list[str]]:
    """按域前缀（首个 ``.`` 之前的段）分组，组内保持给定顺序。

    供目录端点可选地返回分组视图；无 ``.`` 的动作归入其自身名下的域。
    """
    groups: dict[str, list[str]] = {}
    for action in actions:
        domain = action.split(".", 1)[0]
        groups.setdefault(domain, []).append(action)
    return groups

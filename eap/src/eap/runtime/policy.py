"""Policy Engine（docs/02 ①，M3）：模型中心租户策略，在模型网关路由链上强制执行。

租户上下文：入口端点（agents/chat）设 contextvar，任务引擎等无租户的内部调用
不生效（平台内部模式）。策略评估顺序：租户策略按 priority 升序，随后平台默认
（tenant_id=0）；模型链逐层过滤，prompt 上限超限即拒（EAP-7101）。
"""

from __future__ import annotations

import contextvars

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..modelhub.providers import approx_tokens
from ..models import EvalRunRecord, ModelRecord, PolicyRecord

tenant_scope: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "eap_policy_tenant", default=None)

agent_scope: contextvars.ContextVar[str] = contextvars.ContextVar("eap_agent_scope", default="")


class PolicyDenied(RuntimeError):
    """策略拒绝（EAP-7101）：调用方应映射为 403。"""


def set_tenant(tenant_id: int | None) -> contextvars.Token:
    return tenant_scope.set(tenant_id)


def reset_tenant(token: contextvars.Token) -> None:
    tenant_scope.reset(token)


def _policies_for(db: Session, tenant_id: int | None) -> list[PolicyRecord]:
    if tenant_id is None:
        return []
    rows = db.scalars(
        select(PolicyRecord)
        .where(PolicyRecord.enabled == True)  # noqa: E712
        .order_by(PolicyRecord.priority)
    ).all()
    tenant_rows = [p for p in rows if p.tenant_id == tenant_id]
    if tenant_rows:
        return tenant_rows  # 租户有自己的策略时，平台默认不叠加
    return [p for p in rows if p.tenant_id == 0]


def enforce_chain(db: Session, records: list[ModelRecord]) -> list[ModelRecord]:
    """对候选模型链应用策略过滤；过滤后为空 → PolicyDenied。"""
    tenant_id = tenant_scope.get()
    chain = records
    for policy in _policies_for(db, tenant_id):
        cfg = policy.config or {}
        if policy.kind == "model-allowlist":
            allow = set(cfg.get("models") or [])
            chain = [r for r in chain if r.name in allow]
        elif policy.kind == "provider-allowlist":
            allow = set(cfg.get("providers") or [])
            chain = [r for r in chain if r.provider in allow]
    if records and not chain:
        raise PolicyDenied(
            f"EAP-7101 租户 {tenant_id} 的模型策略拒绝了全部候选模型"
            f"（候选 {[r.name for r in records]}），请检查策略中心配置")
    return chain


def check_prompt(db: Session, messages: list[dict]) -> None:
    """单次调用 prompt token 上限（docs/08 §4 的成本策略子项）。"""
    tenant_id = tenant_scope.get()
    for policy in _policies_for(db, tenant_id):
        if policy.kind == "max-prompt-tokens":
            limit = int((policy.config or {}).get("limit") or 0)
            if limit > 0:
                used = sum(approx_tokens(str(m.get("content") or "")) for m in messages)
                if used > limit:
                    raise PolicyDenied(
                        f"EAP-7101 prompt 约 {used} tokens 超过租户 {tenant_id} "
                        f"单次调用上限 {limit}（策略 {policy.name}）")


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def check_tool(db: Session, tool_name: str, risk_level: str) -> None:
    """工具调用策略（v0.6-①）：tool-allowlist 白名单外拒绝（EAP-7102）。"""
    tenant_id = tenant_scope.get()
    for policy in _policies_for(db, tenant_id):
        if policy.kind == "tool-allowlist":
            allow = set((policy.config or {}).get("tools") or [])
            if allow and tool_name not in allow:
                raise PolicyDenied(
                    f"EAP-7102 工具 {tool_name} 不在租户 {tenant_id} 的工具白名单内"
                    f"（策略 {policy.name}）")


def requires_tool_approval(db: Session, tool_name: str, risk_level: str) -> bool:
    """工具风险审批策略（v0.6-①）：tool-risk-approval 的 threshold ≥ 工具风险 → 强制审批。

    与工具自带 requires_approval 的关系：任一为真即走审批门。
    """
    tenant_id = tenant_scope.get()
    tool_risk = _RISK_ORDER.get(risk_level, 0)
    for policy in _policies_for(db, tenant_id):
        if policy.kind == "tool-risk-approval":
            threshold = _RISK_ORDER.get(str((policy.config or {}).get("threshold") or "high"), 2)
            if tool_risk >= threshold:
                return True
    return False


def check_agent_delegation(db: Session, target_agent: str) -> None:
    """多智能体委派边界（v0.6-①）：agent-allowlist 限制可委派的下级智能体。"""
    tenant_id = tenant_scope.get()
    for policy in _policies_for(db, tenant_id):
        if policy.kind == "agent-allowlist":
            allow = set((policy.config or {}).get("agents") or [])
            if allow and target_agent not in allow:
                raise PolicyDenied(
                    f"EAP-7102 智能体 {target_agent} 不在租户 {tenant_id} 的可委派名单内"
                    f"（策略 {policy.name}）")


def check_tool_sandbox(db: Session, tool_name: str, runtime: str = "inproc") -> dict | None:
    """工具沙箱策略（M33 任务组 P2）：kind=tool-sandbox，config
    {"mode": "enforce"|"audit", "tools": [...]}。

    工具命中清单时返回执行决策（调用方按 action 路由；未命中返回 None，
    走原有进程内路径不变）。多条策略命中取 priority 升序第一条：
    - {"action": "sandbox", ...}：脚本工具必须经 SandboxRunner 执行
      （防绕过 handler，enforce/audit 一致）；
    - {"action": "deny", ...}：enforce 下进程内工具无法沙箱化 → 拒绝（EAP-7102）；
    - {"action": "audit", ...}：audit 下进程内工具放行执行，
      但调用方须记 tool.sandbox.violation 审计与指标。
    """
    tenant_id = tenant_scope.get()
    for policy in _policies_for(db, tenant_id):
        if policy.kind != "tool-sandbox":
            continue
        tools = set((policy.config or {}).get("tools") or [])
        if tool_name not in tools:
            continue
        mode = str((policy.config or {}).get("mode") or "enforce")
        if runtime == "script":
            return {"action": "sandbox", "mode": mode, "policy": policy.name}
        if mode == "enforce":
            return {"action": "deny", "mode": mode, "policy": policy.name,
                    "reason": f"工具 {tool_name} 被要求沙箱执行但为进程内实现"
                              f"（策略 {policy.name}，进程内工具无法沙箱化，EAP-7102）"}
        return {"action": "audit", "mode": mode, "policy": policy.name}
    return None


def check_a2a_delegate(db: Session, endpoint: str, target_agent: str = "") -> None:
    """A2A 外部委派边界（M30）：数据出租户边界，fail-closed 默认拒绝，策略显式放行方可执行。

    与 agent-allowlist（M24，默认放行 + 名单收敛）互补：跨租户/外部 endpoint 委派
    只有租户策略 kind=a2a-delegate-allowlist 命中才放行，config：
    {"endpoints": ["https://...", "*"], "agents": ["外部 agent 名", "*"]}
    （endpoint 命中或 agent 命中即放行，"*" 通配）。
    """
    tenant_id = tenant_scope.get()
    endpoint = (endpoint or "").rstrip("/")
    gate_names: list[str] = []
    for policy in _policies_for(db, tenant_id):
        if policy.kind != "a2a-delegate-allowlist":
            continue
        gate_names.append(policy.name)
        cfg = policy.config or {}
        endpoints = {str(e).rstrip("/") for e in (cfg.get("endpoints") or [])}
        agents = {str(a) for a in (cfg.get("agents") or [])}
        if endpoint in endpoints or "*" in endpoints or target_agent in agents or "*" in agents:
            return
    where = f"（策略 {'、'.join(gate_names)}）" if gate_names else "（未配置放行策略）"
    raise PolicyDenied(
        f"EAP-7102 A2A 外部委派默认拒绝：endpoint {endpoint or '∅'} / agent {target_agent or '∅'} "
        f"不在租户 {tenant_id} 的 a2a-delegate-allowlist 放行名单内{where}")


def check_env_protection(db: Session, env: str, actor: str) -> dict | None:
    """环境保护规则判定（M55-E）：工作流 publish/rollback 到受保护环境前的闸门。

    kind=env-protection，config={"rules": [{"env": "prod", "allowed_actors": [...],
    "require_confirm": bool}]}。actor 为 audit.actor_of 形态（"jwt:<user>" / "api-key"），
    与 allowed_actors 逐项精确匹配。

    语义（多条规则命中同一 env 取最严者，conjunction 语义与 GitHub Environments 一致）：
    - 任一命中规则的 allowed_actors 不含 actor → {"action": "deny"}（空名单 = 冻结该环境，
      任何操作者都拒）；
    - 否则任一命中规则 require_confirm=true → {"action": "confirm"}（操作者须显式二次确认）；
    - 无命中 → None（照常放行，存量零影响）。

    与其他 check_* 的租户语义差异：工作流发布/回滚是平台级管理操作（无租户上下文
    contextvar），故直接查询全部启用的 env-protection 策略（跨租户），由平台管理员治理。

    返回 None 或 {"action": "deny"|"confirm", "env": env, "policies": [策略名...]}；
    deny 额外带 "denied_by"（拒绝来源策略名列表）。畸形规则（非 dict / env 非法）跳过不拦。
    """
    from .workflow_versions import ENVS

    rows = db.scalars(
        select(PolicyRecord)
        .where(PolicyRecord.enabled == True,  # noqa: E712
               PolicyRecord.kind == "env-protection")
        .order_by(PolicyRecord.priority)
    ).all()
    policies: list[str] = []
    denied_by: list[str] = []
    need_confirm = False
    for row in rows:
        rules = (row.config or {}).get("rules")
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("env") not in ENVS:
                continue  # 畸形规则防御性跳过（创建侧已校验，运行期不再信任）
            if str(rule["env"]) != env:
                continue  # 环境精确匹配：他环境规则不影响本 env（多环境精确度）
            policies.append(row.name)
            actors = {str(a) for a in (rule.get("allowed_actors") or [])}
            if actor not in actors:
                denied_by.append(row.name)
            elif bool(rule.get("require_confirm")):
                need_confirm = True
    if denied_by:
        return {"action": "deny", "env": env, "policies": sorted(set(policies)),
                "denied_by": sorted(set(denied_by))}
    if need_confirm:
        return {"action": "confirm", "env": env, "policies": sorted(set(policies))}
    return None


def apply_eval_gate(db: Session, records: list[ModelRecord]) -> tuple[list[ModelRecord], list[dict]]:
    """评测门禁（M42-B，docs/10 遗留项）：清单内模型须过评测方可路由，与熔断过滤同位。

    kind=eval-gate，config={"models": [...], "require_eval": bool, "min_pass_rate": 0.8}：
    - 清单内模型：无 model 评测记录且 require_eval=true → 剔除；
      有记录但最新一条 verdict != PASS 或 pass_rate < min_pass_rate → 剔除；
    - 清单外模型不受影响（存量零破坏）；无 eval-gate 策略时原链原样返回。

    返回（过滤后链，被剔除明细 [{model, reason, policy}]）——剔除不抛错，由路由器
    落审计 model.eval_gate.blocked 后自然落到降级链下一个（M31 熔断过滤同模式）。
    """
    tenant_id = tenant_scope.get()
    gates = [p for p in _policies_for(db, tenant_id) if p.kind == "eval-gate"]
    if not gates or not records:
        return records, []
    blocked: list[dict] = []
    chain = list(records)
    for policy in gates:
        cfg = policy.config or {}
        gated = {str(m) for m in (cfg.get("models") or [])}
        if not gated:
            continue
        require_eval = bool(cfg.get("require_eval", True))
        min_rate = float(cfg.get("min_pass_rate") or 0.0)
        kept: list[ModelRecord] = []
        for record in chain:
            if record.name not in gated:
                kept.append(record)
                continue
            latest = db.scalars(
                select(EvalRunRecord)
                .where(EvalRunRecord.model == record.name)
                .order_by(EvalRunRecord.created_at.desc())
                .limit(1)
            ).first()
            if latest is None:
                if require_eval:
                    blocked.append({"model": record.name, "policy": policy.name,
                                    "reason": "清单内模型无评测记录且 require_eval=true"})
                else:
                    kept.append(record)
                continue
            if latest.verdict != "PASS":
                blocked.append({"model": record.name, "policy": policy.name,
                                "reason": f"最新评测 verdict={latest.verdict}（非 PASS）"})
                continue
            if latest.pass_rate is not None and latest.pass_rate < min_rate:
                blocked.append({"model": record.name, "policy": policy.name,
                                "reason": f"评测通过率 {latest.pass_rate} 低于门禁 {min_rate}"})
                continue
            kept.append(record)
        chain = kept
    return chain, blocked

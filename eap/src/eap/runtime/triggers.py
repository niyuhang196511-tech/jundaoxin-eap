"""TriggerRule 引擎（M30 事件中心）：event / cron / webhook → 目标 agent / workflow / connector。

- 启动（lifespan）加载 enabled 规则：source=event 订阅事件总线（支持通配）、
  source=cron 注册进 asyncio 循环（croniter 推算下次执行）、source=webhook 由 API 端点直接触发
- 触发动作复用任务引擎：agent → agent.invoke 任务；workflow → 工作流运行（test_run_async，
  运行记录落 workflow_runs 可轮询）——侵入最小的既有通道复用；connector（M31 任务组 C）→
  解析连接器端点工具同步执行（payload 含 endpoint/arguments）
- 每次触发：min_interval_s 限流（超限丢弃 + 审计 trigger.rate_limited）+ 审计 trigger.fire；
  失败仅记录，不抛出不阻断总线
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import TriggerRuleRecord
from ..observability import audit
from ..security_crypto import decrypt_secret
from .events import bus

log = logging.getLogger("eap.triggers")


@dataclass
class RuleSnapshot:
    """规则内存快照（DB 行的解密后投影，脱离会话使用）。"""

    id: int
    name: str
    tenant_id: int | None
    source: str
    event_type: str = ""
    match: dict = field(default_factory=dict)
    cron: str = ""
    secret: str = ""  # 解密后明文（webhook HMAC 校验用）
    target_type: str = "agent"
    target_name: str = ""
    input_mode: str = "payload"
    template: object = None
    min_interval_s: int = 0


def snapshot_of(rule: TriggerRuleRecord) -> RuleSnapshot:
    """DB 行 → 规则快照（secret 解密）。"""
    return RuleSnapshot(
        id=rule.id, name=rule.name, tenant_id=rule.tenant_id, source=rule.source,
        event_type=rule.event_type or "", match=dict(rule.match or {}),
        cron=rule.cron or "", secret=decrypt_secret(rule.secret) or "",
        target_type=rule.target_type, target_name=rule.target_name,
        input_mode=rule.input_mode, template=rule.template,
        min_interval_s=int(rule.min_interval_s or 0),
    )


# ---------- 模板变量替换（{{var}} 简单点路径取值） ----------

_TEMPLATE_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _lookup(path: str, ctx: dict):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _render(value, ctx: dict):
    """递归渲染：str 内 {{var}} 替换为上下文取值（取不到替换为空串）。"""
    if isinstance(value, str):
        return _TEMPLATE_VAR.sub(lambda m: str(v) if (v := _lookup(m.group(1), ctx)) is not None else "", value)
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


class TriggerEngine:
    """触发引擎：每实例绑定任务引擎（agent 触发经 agent.invoke 任务通道）。"""

    def __init__(self, task_engine) -> None:
        self._tasks = task_engine
        self._rules: dict[int, RuleSnapshot] = {}
        self._event_subs: dict[int, tuple[RuleSnapshot, asyncio.Queue]] = {}
        self._event_tasks: dict[int, asyncio.Task] = {}
        self._cron_rules: dict[int, tuple[RuleSnapshot, datetime | None]] = {}
        self._cron_task: asyncio.Task | None = None
        self._last_fire: dict[int, float] = {}  # 规则 id → 最近触发时刻（monotonic，限流依据）

    # ---------- 生命周期 ----------

    def reload(self) -> int:
        """从 DB 重载 enabled 规则（CRUD 后即时生效；事件订阅重建，cron 重排下次执行）。

        事件消费任务须在事件循环内创建——CRUD 端点均为 async def（循环上执行）；
        若在无循环上下文被调用（防御），仅落 cron/内存态，事件订阅留待下次循环内 reload。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for task in self._event_tasks.values():
            task.cancel()
        self._event_tasks.clear()
        for _rule, queue in self._event_subs.values():
            bus.unsubscribe(queue)
        self._event_subs.clear()
        self._cron_rules.clear()

        rules: list[RuleSnapshot] = []
        with SessionLocal() as db:
            for record in db.scalars(select(TriggerRuleRecord)
                                     .where(TriggerRuleRecord.enabled == True)).all():  # noqa: E712
                try:
                    rules.append(snapshot_of(record))
                except Exception as e:
                    log.warning("规则 %s 快照失败: %s", record.name, e)
        for rule in rules:
            self._rules[rule.id] = rule
            if rule.source == "event":
                if loop is None:
                    log.warning("规则 %s 于无事件循环上下文重载，事件消费待下次循环内 reload 建立", rule.name)
                    continue
                queue = bus.subscribe(rule.event_type or "*")
                self._event_subs[rule.id] = (rule, queue)
                self._event_tasks[rule.id] = asyncio.create_task(self._consume_event(rule, queue))
            elif rule.source == "cron":
                self._cron_rules[rule.id] = (rule, self._next_cron(rule.cron))
        return len(rules)

    async def start(self) -> None:
        self.reload()
        self._cron_task = asyncio.create_task(self._cron_loop())
        log.info("触发引擎启动：%d 条规则（event=%d cron=%d）",
                 len(self._rules), len(self._event_subs), len(self._cron_rules))

    async def stop(self) -> None:
        if self._cron_task is not None:
            self._cron_task.cancel()
            try:
                await self._cron_task
            except asyncio.CancelledError:
                pass
            self._cron_task = None
        for task in self._event_tasks.values():
            task.cancel()
        await asyncio.gather(*self._event_tasks.values(), return_exceptions=True)
        self._event_tasks.clear()
        for _rule, queue in self._event_subs.values():
            bus.unsubscribe(queue)
        self._event_subs.clear()
        self._cron_rules.clear()
        self._rules.clear()

    # ---------- 触发 ----------

    async def fire(self, rule: RuleSnapshot, source: str, data: dict) -> dict:
        """执行一次触发：限流 → 构造输入 → 提交目标 → 审计 trigger.fire。

        返回 {rule, source, status, ref}；被限流/失败时 status=dropped/error 且 ref 为空。
        任何异常不上抛（不阻断总线/端点）。
        """
        try:
            now = time.monotonic()
            if rule.min_interval_s > 0:
                last = self._last_fire.get(rule.id)
                if last is not None and now - last < rule.min_interval_s:
                    audit.record("trigger.rate_limited", actor="system", target=rule.name,
                                 detail={"source": source, "min_interval_s": rule.min_interval_s})
                    log.info("规则 %s 触发间隔不足（%ds），已丢弃", rule.name, rule.min_interval_s)
                    return {"rule": rule.name, "source": source, "status": "dropped", "ref": ""}
                self._last_fire[rule.id] = now
            payload_input = self._build_input(rule, data)
            if rule.target_type == "workflow":
                from ..workflows import test_run_async  # 延迟导入避免环

                ref = await test_run_async(rule.target_name, payload_input)
                status = "submitted"
            elif rule.target_type == "connector":
                # M31 任务组 C：连接器触发——解析端点工具并同步执行，结果摘要作 ref
                ref = await self._fire_connector(rule, payload_input)
                status = "ok"
            else:
                payload = {"agent": rule.target_name, "input": payload_input}
                if rule.tenant_id:
                    payload["_tenant_id"] = rule.tenant_id
                with SessionLocal() as db:
                    ref = await self._tasks.submit(db, "agent.invoke", payload)
                status = "submitted"
        except Exception as e:
            status, ref = "error", ""
            log.warning("触发规则 %s 失败: %s", rule.name, e)
        detail: dict = {"source": source, "target": f"{rule.target_type}:{rule.target_name}",
                        "status": status, "ref": ref}
        if rule.target_type == "connector":
            detail["connector"] = rule.target_name  # 审计标 connector（M31 任务组 C）
        audit.record("trigger.fire", actor="system", target=rule.name, detail=detail)
        return {"rule": rule.name, "source": source, "status": status, "ref": ref}

    async def _fire_connector(self, rule: RuleSnapshot, payload_input: str) -> str:
        """target_type=connector：payload 需含 endpoint（工具名或端点名）与 arguments。

        经 load_connector_tools/endpoint_tool 解析执行（复用 OAuth/白名单/事件通道）；
        返回结果 JSON 前 500 字符作审计 ref。任何失败上抛由 fire() 统一转 error 审计。
        """
        from ..models import ConnectorRecord
        from .connectors import load_connector_tools
        from .tools import find_tool

        try:
            data = json.loads(payload_input or "{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {"arguments": data}
        endpoint = str(data.get("endpoint") or "")
        arguments = data.get("arguments")
        with SessionLocal() as db:
            record = db.scalar(select(ConnectorRecord)
                               .where(ConnectorRecord.name == rule.target_name))
            if record is None:
                raise ValueError(f"EAP-4004 连接器 {rule.target_name} 不存在")
            tools = load_connector_tools(record)  # 未启用 → 空
        tool = find_tool(tools, endpoint)
        if tool is None and endpoint:
            # 容错：允许只写端点名（按工具名后缀匹配，如 erp.inventory.query）
            tool = next((t for t in tools if t.name.endswith("." + endpoint)), None)
        if tool is None:
            raise ValueError(f"EAP-4004 连接器 {rule.target_name} 无可用端点 {endpoint!r}（或未启用）")
        result = await tool.handler(json.dumps(arguments if arguments is not None else {},
                                               ensure_ascii=False))
        return result[:500]

    def _build_input(self, rule: RuleSnapshot, data: dict) -> str:
        """触发输入：payload 模式透传触发数据（dict 序列化为 JSON）；template 模式变量替换。"""
        if rule.input_mode == "template" and rule.template is not None:
            ctx = {"data": data, "event": data, "trigger": {"name": rule.name, "source": rule.source}}
            rendered = _render(rule.template, ctx)
            if isinstance(rendered, str):
                return rendered
            if isinstance(rendered, dict) and "input" in rendered:
                return str(rendered["input"])
            return json.dumps(rendered, ensure_ascii=False)
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False)
        return str(data)

    # ---------- event 来源 ----------

    async def _consume_event(self, rule: RuleSnapshot, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            try:
                data = dict(event.get("data") or {})
                data.setdefault("event_type", event.get("type"))
                data.setdefault("_event_id", event.get("id"))
                if self._match_ok(rule.match, data):
                    await self.fire(rule, "event", data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("事件规则 %s 消费异常: %s", rule.name, e)

    @staticmethod
    def _match_ok(match: dict | None, data: dict) -> bool:
        """match 为简单等值过滤：data 顶层键值全等才触发。"""
        if not match:
            return True
        return all(data.get(k) == v for k, v in match.items())

    # ---------- cron 来源 ----------

    @staticmethod
    def _next_cron(cron: str, base: datetime | None = None) -> datetime | None:
        """croniter 推算下次执行（UTC，naive）；坏表达式返回 None（本周期跳过，告警可见）。"""
        if not cron:
            return None
        from croniter import croniter

        try:
            base = base or datetime.now(timezone.utc)
            return croniter(cron, base.replace(tzinfo=None)).get_next(datetime).replace(tzinfo=None)
        except Exception as e:
            log.warning("cron %r 无效（本周期跳过）: %s", cron, e)
            return None

    async def _cron_loop(self) -> None:
        while True:
            try:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                for rid, (rule, next_run) in list(self._cron_rules.items()):
                    if next_run is not None and now >= next_run:
                        # 先推进下次执行再触发（失败不空转重试，靠 min_interval_s 与下轮兜底）
                        self._cron_rules[rid] = (rule, self._next_cron(rule.cron, now)
                                                 or now + timedelta(days=1))
                        asyncio.create_task(self.fire(rule, "cron", {"cron": rule.cron}))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("cron 触发循环异常: %s", e)
            await asyncio.sleep(1)


__all__ = ["TriggerEngine", "RuleSnapshot", "snapshot_of"]

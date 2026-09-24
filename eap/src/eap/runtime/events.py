"""进程内异步事件总线（M30 事件中心底座）：发布/订阅 + 通配匹配 + 可选 Redis 跨副本。

- 订阅者持有一个 asyncio.Queue，emit 非阻塞 fan-out（队列满丢弃并告警，绝不阻断业务）
- 事件结构：{id, type, tenant_id, data, ts}
- EAP_REDIS_URL 配置时经 Redis pub/sub 跨副本转发（发布端 fan-out 本地 + 转发；
  订阅端收消息转投本地总线）；未配置为纯进程内（离线/测试零依赖）
- emit_event()：同步/异步上下文通用的便捷发射（异常吞掉仅告警，不阻断业务）
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
import uuid

log = logging.getLogger("eap.events")

CHANNEL = "eap:events"  # Redis 跨副本转发频道
QUEUE_MAX = 256  # 单订阅者队列深度（满则丢弃，背压不上抛）

# ---------- 事件目录（M49-E1） ----------
# 目录 = 文档性质：全仓 emit_event 实际发射点（grep 核实）的枚举，供触发器/
# Webhook 订阅表单做选项提示（GET /api/v1/triggers/event-types）。
# 订阅/触发仍按 fnmatch 通配匹配（task.* 等），目录不构成白名单校验，
# 新增事件无需先改目录即可被订阅。
EVENT_CATALOG: tuple[str, ...] = (
    "agent.run.completed",    # api/v1/agents.py：智能体运行完成（同步与 SSE 流式）
    "kb.document.indexed",    # api/v1/kb.py：知识库文档索引完成
    "workflow.run.finished",  # workflows.py：工作流运行结束
    "connector.invoked",      # runtime/connectors.py：连接器出站调用
    "task.completed",         # runtime/tasks.py：任务终态 COMPLETED
    "task.failed",            # runtime/tasks.py：任务终态 FAILED
    # 注：webhook.test 为手工试投专用合成事件（runtime/webhooks.test_push 直接
    # 投递、不经总线与 pattern 匹配），不可被订阅，故不列入目录。
)


class EventBus:
    """进程内事件总线：subscribe(pattern) → Queue；await emit(...) 非阻塞 fan-out。"""

    def __init__(self) -> None:
        self._subs: dict[int, tuple[str, asyncio.Queue]] = {}
        self._sub_seq = 0
        self._redis = None  # redis.asyncio.Redis | None
        self._listener: asyncio.Task | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None  # bus.start 所在循环（跨线程投递依据）

    # ---------- 订阅 ----------

    def subscribe(self, event_pattern: str = "*", maxsize: int = QUEUE_MAX) -> asyncio.Queue:
        """订阅事件模式（支持 task.* 通配，fnmatch 语义）；返回事件队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, maxsize))
        self._sub_seq += 1
        self._subs[self._sub_seq] = (event_pattern or "*", queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        for sid, (_pattern, q) in list(self._subs.items()):
            if q is queue:
                self._subs.pop(sid, None)

    # ---------- 发布 ----------

    @staticmethod
    def build_event(event_type: str, tenant_id: int | None = None, data: dict | None = None) -> dict:
        return {"id": uuid.uuid4().hex, "type": event_type, "tenant_id": tenant_id,
                "data": data or {}, "ts": time.time()}

    def emit_local(self, event: dict) -> int:
        """投递到本地匹配订阅者（非阻塞；队列满丢弃并告警）。返回投递数。"""
        delivered = 0
        for _sid, (pattern, queue) in list(self._subs.items()):
            if not fnmatch.fnmatchcase(event.get("type", ""), pattern):
                continue
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                log.warning("订阅队列已满，丢弃事件 %s（id=%s）", event.get("type"), event.get("id"))
        return delivered

    async def emit(self, event_type: str, tenant_id: int | None = None, data: dict | None = None) -> dict:
        """发射事件：本地 fan-out + （配置 Redis 时）跨副本转发。"""
        event = self.build_event(event_type, tenant_id, data)
        self.emit_local(event)
        if self._redis is not None:
            try:
                await self._redis.publish(CHANNEL, json.dumps(event, ensure_ascii=False, default=str))
            except Exception as e:
                log.warning("Redis 事件转发失败（本地已投递）: %s", e)
        return event

    # ---------- 生命周期 / Redis 跨副本 ----------

    async def start(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        url = None
        try:
            from ..config import get_settings

            url = get_settings().redis_url
        except Exception:
            pass
        if not url:
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(url, decode_responses=True)
            self._listener = asyncio.create_task(self._listen())
            log.info("事件总线跨副本转发已启用（Redis pub/sub %s）", CHANNEL)
        except Exception as e:
            log.warning("Redis 不可用，事件总线退化为纯进程内: %s", e)
            self._redis = None

    async def _listen(self) -> None:
        """订阅端：收 Redis 消息转投本地总线（只 emit_local，不再转发，避免回环）。"""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    try:
                        event = json.loads(message["data"])
                        if isinstance(event, dict) and event.get("type"):
                            self.emit_local(event)
                    except (ValueError, TypeError):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Redis 事件订阅异常，1s 后重试: %s", e)
                await asyncio.sleep(1)

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
        self._main_loop = None


bus = EventBus()


def emit_event(event_type: str, tenant_id: int | None = None, data: dict | None = None) -> None:
    """便捷发射（同步/异步上下文皆可用）：异常吞掉仅告警，不阻断业务。

    - 有运行循环 → create_task 发射；无循环但有总线主循环（如线程池里的同步端点）
      → call_soon_threadsafe 调度到主循环；两者皆无（纯脚本/测试）→ 直接本地投递
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(bus.emit(event_type, tenant_id, data))
            return
        main = bus._main_loop
        if main is not None and main.is_running():
            main.call_soon_threadsafe(
                lambda: main.create_task(bus.emit(event_type, tenant_id, data)))
        else:
            bus.emit_local(bus.build_event(event_type, tenant_id, data))
    except Exception as e:
        log.warning("事件 %s 发射失败（已忽略）: %s", event_type, e)

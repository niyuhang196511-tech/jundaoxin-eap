"""Registry 跨副本事件广播（M10）：管理操作在所有副本生效。

多副本形态下，Agent 目录的分发是"确定性派生 + 事件驱动"：
- 目录来源本身各副本一致：builtin/plugins/entry_points（同代码）+ workflows（DB 对账，启动加载）
- 不一致的只有**运行态管理操作**（start/stop/unregister/reload）——本模块把这些操作
  经 Redis Pub/Sub 广播，各副本订阅后本地应用。
- 无 Redis（单机形态）时发布是 no-op，行为与原来完全一致。
- 投递语义：at-most-once（Pub/Sub）。错过的消息由 DB 状态对账兜底：目录拉起以 DB/代码为
  准（bootstrap/load_enabled），运行态（stopped/unhealthy）由 /agents 目录与 DB 差异对账
  周期性收敛——管理语义最终一致。

身份微服务迁出预留：本通道与鉴权无关，迁出认证服务后本模块不动。
"""

from __future__ import annotations

import asyncio
import json
import logging

from ..config import get_settings

logger = logging.getLogger("eap.registry-sync")

_CHANNEL = "eap:registry-events"
_STATE: dict = {"task": None}


def _redis_url() -> str | None:
    return get_settings().redis_url or None


async def publish(event: dict) -> None:
    """广播管理事件（无 Redis / 发送失败静默——对账兜底）。"""
    url = _redis_url()
    if not url:
        return
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(url, decode_responses=True)
        try:
            await r.publish(_CHANNEL, json.dumps(event, ensure_ascii=False))
        finally:
            await r.aclose()
    except Exception as e:
        logger.debug("registry 事件发布失败（对账兜底）: %s", e)


def publish_sync(event: dict) -> None:
    """同步上下文的发布（FastAPI async 端点内通常用 publish 即可）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(publish(event))
        return
    loop.create_task(publish(event))


async def subscribe_and_apply(apply_fn) -> None:
    """订阅事件并逐条应用（apply_fn: event -> Awaitable）。阻塞协程，由启动方建任务托管。"""
    url = _redis_url()
    if not url:
        return
    import redis.asyncio as aioredis

    r = aioredis.from_url(url, decode_responses=True)
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                event = json.loads(message.get("data") or "{}")
                result = apply_fn(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("registry 事件应用失败: %s", e)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("registry 订阅断开（对账兜底）: %s", e)
    finally:
        try:
            await pubsub.aclose()
            await r.aclose()
        except Exception:
            pass


def start_subscriber(apply_fn) -> None:
    """有 Redis 时启动订阅任务（幂等；模块级单任务托管）。"""
    if not _redis_url() or _STATE["task"] is not None:
        return
    _STATE["task"] = asyncio.create_task(subscribe_and_apply(apply_fn))


async def stop_subscriber() -> None:
    task = _STATE["task"]
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _STATE["task"] = None

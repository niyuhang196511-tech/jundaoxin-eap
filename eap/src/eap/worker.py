"""python -m eap.worker：独立任务 Worker 进程（M32 任务组 P1，v0.9 生产化）。

与 API 进程分离部署的常驻 worker：只读任务引擎相关配置（不设 host/port 等 API 项），
不导入 eap.main（避免拉起 FastAPI/路由依赖）。职责：

- 启动：init_db（空库自动建表/迁移）→ 打印版本与关键配置 → 启动 TaskEngine
  （worker 数 = EAP_WORKER_COUNT，默认 2）消费任务队列
- 停止：SIGINT/SIGTERM 优雅停——先停领取新任务并等待在途完成（drain，≤30s），
  再停引擎（超时未完成的在途任务回退 PENDING 并清租约，重启/其他副本续跑，不丢失）
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import get_settings
from .runtime.tasks import create_task_engine

log = logging.getLogger("eap.worker")

_DRAIN_TIMEOUT_S = 30.0  # 优雅停等待在途任务完成的超时（超时后取消并回退 PENDING）


def _version() -> str:
    from . import __version__

    return __version__


def _safe_db(db_url: str) -> str:
    """日志中的连接串脱敏：剥离密码（postgresql://user:pass@host → user:***@host）。"""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(db_url)
        if parts.password is None:
            return db_url
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, f"{parts.username}:***@{host}", parts.path, "", ""))
    except Exception:
        return "<db>"


async def _serve() -> None:
    s = get_settings()
    from .db import init_db

    init_db()
    # M55-A（双进程压测发现）：独立 worker 同样需要注册引导——registry.bootstrap
    # 原先只在 API 进程 lifespan 执行，HA 形态（API EAP_WORKER_COUNT=0 + 独立 worker，
    # deploy/docker-compose.ha.yml）下 worker 注册表为空，agent.invoke/hitl 任务
    # 全数 FAILED（"智能体 faq-agent 未注册"）。与 lifespan 同序：init_db → bootstrap → 引擎。
    from .agents.registry import registry

    await registry.bootstrap()
    engine = create_task_engine()
    await engine.start(workers=s.worker_count)
    log.info("EAP worker v%s 就绪：workers=%d lease=%ds backend=%s db=%s",
             _version(), s.worker_count, s.worker_lease_seconds,
             type(engine._backend).__name__, _safe_db(s.db_url))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        if not stop_event.is_set():
            log.info("收到停止信号：停止领取新任务，等待在途任务完成（≤%.0fs）", _DRAIN_TIMEOUT_S)
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError, RuntimeError):
            # Windows ProactorEventLoop 不支持 add_signal_handler：回退 signal.signal（主线程）
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_request_stop))

    await stop_event.wait()
    await engine.drain(timeout=_DRAIN_TIMEOUT_S)  # 第一阶段：停入队、等在途完成
    await engine.stop()  # 第二阶段：停 worker/调度/租约扫描/队列后端
    log.info("worker 已退出")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    log.info("EAP worker 启动 v%s（workers=%d，lease=%ds，redis=%s）",
             _version(), s.worker_count, s.worker_lease_seconds,
             "配置" if s.redis_url else "未配置（单机内存队列）")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()

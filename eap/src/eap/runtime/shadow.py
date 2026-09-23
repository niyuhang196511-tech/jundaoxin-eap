"""在线影子流量（M44-A，L10 评测深化，docs/08 §3 评测体系的线上延伸）。

机制：生产 agent 调用完成点（API 同步 + SSE 两通道）按配置抽样率把**同一输入**
异步发往影子 agent（候选版本），全量执行但输出不返回用户，与主调用配对落
shadow_runs（两侧输出/延迟），供报表对比（/api/v1/evals/shadow-configs/{name}/report）。

边界（如实）：
- 影子调用 fire-and-forget，任何失败只落 shadow_runs.shadow_error，绝不影响主调用；
- 独立 DB 会话 + 独立策略租户上下文（后台任务脱离请求生命周期）；
- 影子 trace_id = f"{主trace}-shadow"：token/成本经 usage_records 独立归属，
  不与主调用混计（影子流量有真实成本，配置人须知）；
- 不经限流与预算熔断（内部调用）；任务通道（agent.invoke 任务）不挂钩，
  仅 API 调用通道（同步 + SSE）——任务通道的输入在 payload 内，形态不同，后置；
- 交互挂起（WAITING_INPUT）的主/影调用不产生可对比记录（挂起态没有完整输出）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ShadowConfigRecord, ShadowRunRecord
from ..schemas import InvokeRequest

log = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """后台影子任务的兜底日志：任务本体永不抛错，此处只防未知异常静默丢失。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("影子流量后台任务异常（已与主调用隔离）：%s", exc)


def launch_for(db: Session, source_agent: str, body: InvokeRequest, trace_id: str,
               tenant_id: int | None, primary_output: str, primary_latency_ms: int) -> None:
    """主调用完成点挂钩：查启用配置 → 抽样判定 → 后台任务执行影子调用。

    同步部分只做配置查询与抽样判定（低开销，跟随请求生命周期）；
    耗时的影子执行全在 asyncio 后台任务。任何异常不向上传播。
    调用方须保证主调用已完成且非交互挂起（interaction is None）。
    """
    try:
        if not trace_id:
            return
        cfgs = db.scalars(select(ShadowConfigRecord).where(
            ShadowConfigRecord.source_agent == source_agent,
            ShadowConfigRecord.enabled == True)).all()  # noqa: E712
        for cfg in cfgs:
            if random.random() >= cfg.sample_rate:  # sample_rate=0 恒跳过，1.0 恒执行
                continue
            request_data = {"input": body.input, "session_id": body.session_id,
                            "user_id": body.user_id, "output_schema": body.output_schema}
            task = asyncio.get_running_loop().create_task(_run_shadow(
                cfg.id, cfg.name, source_agent, cfg.shadow_agent, request_data,
                trace_id, tenant_id, primary_output, primary_latency_ms))
            task.add_done_callback(_log_task_exception)
    except Exception:  # 影子分流的准备阶段也不得影响主调用
        pass


async def _run_shadow(config_id: int, config_name: str, source_agent: str,
                      shadow_agent: str, request_data: dict, trace_id: str,
                      tenant_id: int | None,
                      primary_output: str, primary_latency_ms: int) -> None:
    """后台影子执行：独立会话 + 独立租户上下文 → registry.invoke → 配对落库。"""
    from . import policy  # eap.runtime.policy
    from ..agents.registry import registry  # eap.agents.registry

    db = SessionLocal()
    t0 = time.monotonic()
    shadow_trace = f"{trace_id}-shadow"
    output, error = "", ""
    token = policy.set_tenant(tenant_id)
    try:
        resp = await registry.invoke(
            db, shadow_agent,
            InvokeRequest(input=request_data["input"], stream=False,
                          session_id=request_data.get("session_id"),
                          user_id=request_data.get("user_id"),
                          output_schema=request_data.get("output_schema")),
            trace_id=shadow_trace)
        output = resp.output or ""
        if resp.interaction is not None:  # 影子侧也挂起交互：如实记录为不可对比
            error = "影子调用挂起交互（无完整输出）"
            output = ""
    except Exception as e:  # 影子 agent 缺失/执行失败等一律记录，不抛出
        error = str(e)[:500]
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        db.add(ShadowRunRecord(
            config_id=config_id, config_name=config_name, trace_id=trace_id,
            source_agent=source_agent, shadow_agent=shadow_agent,
            input_text=str(request_data.get("input", ""))[:4000],
            primary_output=(primary_output or "")[:8000],
            primary_latency_ms=primary_latency_ms,
            shadow_output=output[:8000], shadow_latency_ms=latency_ms,
            shadow_error=error))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        policy.reset_tenant(token)
        db.close()

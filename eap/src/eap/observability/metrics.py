"""可观测性（M7）：进程内指标计数器 + Prometheus exposition 格式 /metrics。

无第三方依赖（不引 prometheus-client）；计数器语义为进程生命周期累计值，
生产多副本由采集端按实例聚合。

M44-C（P4 剩余工作，docs/progress-plan.md 任务组 P4「剩余工作」）：
- histogram 支持：``observe`` + ``HISTOGRAMS`` 注册表——请求延迟直方图分桶导出
  （``_bucket``/``_sum``/``_count``），使 p95 分位数告警（histogram_quantile）成为可能；
  此前延迟仅有均值代理（``eap_request_latency_seconds_sum`` 计数器，已并入 histogram 的
  ``_sum``，避免同名 _sum 双 TYPE 冲突）；
- 任务队列深度 gauge：``eap_task_queue_depth{state}``——``refresh_task_queue_depth``
  在 /metrics 抓取时惰性查 TaskRecord 按 state 计数（取数方案取舍见其 docstring）。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_counters: dict[str, float] = {}

# 指标名 → (help, type)
METRICS: dict[str, tuple[str, str]] = {
    "eap_requests_total": ("Total HTTP requests by method/path/status.", "counter"),
    "eap_agent_invocations_total": ("Agent invocations by agent/status.", "counter"),
    "eap_tasks_total": ("Task executions by type/state.", "counter"),
    "eap_tokens_total": ("Token usage by direction (in/out).", "counter"),
    # API Gateway（M31 任务组 F）
    "eap_gateway_rejected_total": ("Gateway rejected requests by reason (concurrency/body_size/timeout).", "counter"),
    "eap_idempotent_replays_total": ("Responses replayed via Idempotency-Key.", "counter"),
    "eap_gateway_inflight": ("Currently in-flight requests gateway-wide.", "gauge"),
    "eap_circuit_state": ("Circuit breaker state per model (0 closed / 1 open / 2 half-open).", "gauge"),
    "eap_circuit_trips_total": ("Circuit breaker trips per model.", "counter"),
    # Sandbox（M33 任务组 P2）
    "eap_sandbox_exec_total": ("Sandbox script executions by result (ok/timeout/error).", "counter"),
    "eap_sandbox_violations_total": ("Sandbox policy violations by mode (enforce/audit).", "counter"),
    # 任务队列深度（M44-C / P4 剩余工作）：按 state 的 gauge，值由 /metrics 抓取时刷新
    "eap_task_queue_depth": ("Task records in DB by state (PENDING/RUNNING/...) at scrape time.", "gauge"),
}

# 直方图注册表（M44-C / P4 剩余工作）：名 → (help, 桶上界升序)
# 桶取值覆盖 0.01s~60s 数量级（网关秒拒 → 模型调用分钟级），与告警 p95 阈值（2~5s）
# 同一量级内插值精度充足。刻意不预置部署可配——样例固定桶，真实环境按流量形状调整。
HISTOGRAMS: dict[str, tuple[str, tuple[float, ...]]] = {
    "eap_request_latency_seconds": (
        "Request latency in seconds by method/route (route is the route template; "
        "unmatched covers gateway-rejected/unrouted requests; p95-capable).",
        (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    ),
}

# 任务状态机全集（docs/03 §5 的 M1 子集，对齐 runtime/tasks.py 字面量）：
# 已知 state 显式补零导出（缺序列时 Prometheus 查询按空处理，补零利于仪表盘连续性）。
TASK_STATES = ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT",
               "COMPLETED", "FAILED", "CANCELLED")


def incr(metric: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    """计数器累加（labels 决定时间序列；未知 metric 忽略）。"""
    if metric not in METRICS:
        return
    label_str = ""
    if labels:
        pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        label_str = "{" + pairs + "}"
    with _lock:
        _counters[f"{metric}{label_str}"] = _counters.get(f"{metric}{label_str}", 0.0) + value


def gauge_set(metric: str, labels: dict[str, str] | None, value: float) -> None:
    """gauge 置当前值（覆盖语义，区别于 incr 累加；未知 metric 忽略）。"""
    if metric not in METRICS:
        return
    label_str = ""
    if labels:
        pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        label_str = "{" + pairs + "}"
    with _lock:
        _counters[f"{metric}{label_str}"] = float(value)


def observe(metric: str, value: float, labels: dict[str, str] | None = None) -> None:
    """直方图观测（M44-C）：按上界累计进各桶（le 为累计上界，Prometheus histogram 语义），
    并累加 ``_sum``/``_count``。桶序恒定、labels 决定序列基数——调用方需保证低基数
    （如 route 模板而非原始路径）。未知 metric 忽略。"""
    spec = HISTOGRAMS.get(metric)
    if spec is None:
        return
    _help_text, buckets = spec

    def _key(suffix: str, extra: dict[str, str] | None = None) -> str:
        merged = dict(labels or {})
        if extra:
            merged.update(extra)
        label_str = ""
        if merged:
            pairs = ",".join(f'{k}="{v}"' for k, v in sorted(merged.items()))
            label_str = "{" + pairs + "}"
        return f"{metric}{suffix}{label_str}"

    with _lock:
        for upper in buckets:
            if value <= upper:  # 累计语义：观测值 ≤ 桶上界即计入该桶
                key = _key("_bucket", {"le": repr(float(upper))})
                _counters[key] = _counters.get(key, 0.0) + 1.0
        key = _key("_bucket", {"le": "+Inf"})
        _counters[key] = _counters.get(key, 0.0) + 1.0
        key = _key("_sum")
        _counters[key] = _counters.get(key, 0.0) + value
        key = _key("_count")
        _counters[key] = _counters.get(key, 0.0) + 1.0


def refresh_task_queue_depth() -> None:
    """任务队列深度 gauge（M44-C / P4 剩余工作）：按 TaskRecord.state 分组计数。

    取数方案取舍（诚实说明）：
    - 选「抓取时惰性查询 DB」而非引擎周期刷新：gauge 永远反映抓取瞬间的 DB 真值，
      无后台任务生命周期要管理（启动/停机/多副本各自刷新会互相覆盖同库视图）；
      引擎在途内存计数只覆盖本进程正在执行的 RUNNING，表达不了「PENDING 堆积」。
    - 代价：每次 /metrics 抓取多一条 GROUP BY 轻量查询（抓取间隔秒~分钟级，可接受）。
    - 已知 state 显式补零（TASK_STATES），DB 查询失败静默保留上次值（观测不阻断 /metrics）。
    """
    try:
        from sqlalchemy import func, select

        from ..db import SessionLocal
        from ..models import TaskRecord

        with SessionLocal() as db:
            rows = db.execute(
                select(TaskRecord.state, func.count()).group_by(TaskRecord.state)).all()
        counts = {state: float(n) for state, n in rows}
        for state in TASK_STATES:
            gauge_set("eap_task_queue_depth", {"state": state}, counts.get(state, 0.0))
    except Exception:
        pass  # DB 不可用时保留上一次值；观测失败不阻断 /metrics 端点


def render() -> str:
    """Prometheus text exposition 格式输出（计数器/gauge + histogram 族）。"""
    lines: list[str] = []
    with _lock:
        snapshot = dict(_counters)
    for name, (help_text, mtype) in METRICS.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for key in sorted(snapshot):
            if key == name or key.startswith(name + "{"):
                lines.append(f"{key} {snapshot[key]}")
    # histogram 族（M44-C）：TYPE/HELP 挂在基名上，_bucket/_sum/_count 系列紧随
    # （ exposition 规范：histogram 不得对 _sum/_count 单独声明 TYPE，否则解析冲突——
    #   这也是 legacy eap_request_latency_seconds_sum 计数器被并入本族的原因）。
    for name, (help_text, _buckets) in HISTOGRAMS.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} histogram")
        for suffix in ("_bucket", "_sum", "_count"):
            prefix = name + suffix
            for key in sorted(snapshot):
                if key.startswith(prefix + "{"):
                    lines.append(f"{key} {snapshot[key]}")
    return "\n".join(lines) + "\n"

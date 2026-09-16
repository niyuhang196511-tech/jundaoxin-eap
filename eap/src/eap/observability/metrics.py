"""可观测性（M7）：进程内指标计数器 + Prometheus exposition 格式 /metrics。

无第三方依赖（不引 prometheus-client）；计数器语义为进程生命周期累计值，
生产多副本由采集端按实例聚合。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_counters: dict[str, float] = {}

# 指标名 → (help, type)
METRICS: dict[str, tuple[str, str]] = {
    "eap_requests_total": ("Total HTTP requests by method/path/status.", "counter"),
    "eap_request_latency_seconds_sum": ("Cumulative request latency in seconds.", "counter"),
    "eap_agent_invocations_total": ("Agent invocations by agent/status.", "counter"),
    "eap_tasks_total": ("Task executions by type/state.", "counter"),
    "eap_tokens_total": ("Token usage by direction (in/out).", "counter"),
}


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


def render() -> str:
    """Prometheus text exposition 格式输出。"""
    lines: list[str] = []
    with _lock:
        snapshot = dict(_counters)
    for name, (help_text, mtype) in METRICS.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for key in sorted(snapshot):
            if key == name or key.startswith(name + "{"):
                lines.append(f"{key} {snapshot[key]}")
    return "\n".join(lines) + "\n"

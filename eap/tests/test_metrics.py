"""M7 可观测性测试：/metrics exposition + 计数器语义。

M44-C（P4 剩余工作）：请求延迟 histogram 中间件打点 + 任务队列深度 gauge 导出。
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from .conftest import AUTH


@pytest.fixture(scope="module", autouse=True)
def _cleanup_policies(client: TestClient):
    """模块结束后差量清理本模块新增策略行（样板=test_tool_governance/_cleanup_gov_policies）。

    M52-D：策略按 kind 检索且「租户自有策略不叠加平台默认」——本文件的
    model-allowlist 策略若残留（即便已停用），仍会参与后续文件的策略组合语义，
    属跨文件语义污染，删行而非仅停用。"""
    from eap.db import SessionLocal
    from eap.models import PolicyRecord

    with SessionLocal() as db:
        before = set(db.scalars(select(PolicyRecord.id)).all())
    yield
    with SessionLocal() as db:
        for r in db.scalars(select(PolicyRecord)).all():
            if r.id not in before:
                db.delete(r)
        db.commit()


def _counter(body: str, series: str) -> float:
    for line in body.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _series(body: str, name: str) -> list[tuple[dict[str, str], float]]:
    """解析带标签序列：``name{k="v",...} value`` → [(labels, value)]。

    本文件各指标标签值不含逗号/引号转义，按逗号直切即可（解析器刻意保持最小实现）。
    """
    out: list[tuple[dict[str, str], float]] = []
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        head, _, val = line.rpartition(" ")
        inner = head[head.index("{") + 1: head.rindex("}")]
        labels: dict[str, str] = {}
        if inner:
            for part in inner.split(","):
                k, _, v = part.partition("=")
                labels[k] = v.strip('"')
        out.append((labels, float(val)))
    return out


def test_metrics_exposition_format(client: TestClient):
    """/metrics 输出 Prometheus 格式；请求与 agent 调用计入计数器。"""
    client.get("/api/v1/models", headers=AUTH)
    client.get("/health")

    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# TYPE eap_requests_total counter" in body
    assert 'eap_requests_total{method="GET",path="/api/v1/models",status="200"}' in body
    assert "# TYPE eap_agent_invocations_total counter" in body


def test_metrics_counters_increment(client: TestClient):
    """同一指标随请求累加。"""
    series = 'eap_requests_total{method="GET",path="/health",status="200"}'
    before = _counter(client.get("/metrics").text, series)
    client.get("/health")
    client.get("/health")
    after = _counter(client.get("/metrics").text, series)
    assert after >= before + 2


def test_metrics_latency_histogram(client: TestClient):
    """M44-C：请求经 RequestLatencyMiddleware 计入 eap_request_latency_seconds histogram。

    校验 exposition 形态：# TYPE histogram、桶累计单调不减、+Inf 桶 == _count、_sum > 0；
    route 标签为路由模板（/health），method 固定小集合——无原始路径高基数。
    """
    client.get("/health")
    client.get("/health")
    body = client.get("/metrics").text

    assert "# TYPE eap_request_latency_seconds histogram" in body
    assert ('eap_request_latency_seconds_bucket{le="+Inf",method="GET",route="/health"}'
            in body), "labels 应为 method+route（路由模板），le 升序在首"

    buckets = {float(l["le"]): v for l, v in _series(body, "eap_request_latency_seconds_bucket")
               if l.get("route") == "/health" and l.get("method") == "GET"}
    counts = [v for l, v in _series(body, "eap_request_latency_seconds_count")
              if l.get("route") == "/health" and l.get("method") == "GET"]
    sums = [v for l, v in _series(body, "eap_request_latency_seconds_sum")
            if l.get("route") == "/health" and l.get("method") == "GET"]

    assert buckets, "/health 的桶序列应存在"
    assert float("inf") in buckets
    ordered = [buckets[k] for k in sorted(k for k in buckets if k != float("inf"))]
    assert ordered == sorted(ordered), "桶为累计上界语义，计数须单调不减"
    assert len(counts) == 1 and counts[0] >= 2, "_count 每序列唯一且随请求累计"
    assert buckets[float("inf")] == counts[0], "+Inf 桶与 _count 恒等（histogram 语义）"
    assert len(sums) == 1 and sums[0] > 0, "_sum 累计观测值（秒）"


def test_metrics_task_queue_depth_gauge(client: TestClient):
    """M44-C：eap_task_queue_depth{state} 在 /metrics 抓取时按 TaskRecord.state 惰性计数。

    直接落库 PENDING×2 + RUNNING×1（不经 submit 入队，避免引擎调度干预），
    断言导出值与 DB 分组计数一致；引擎后台若恰在窗口内迁移其他用例遗留任务，
    短暂重试至一致（离线确定，不依赖外网/真实 Redis）。
    """
    from sqlalchemy import delete, func, select

    from eap.db import SessionLocal
    from eap.models import TaskRecord
    from eap.observability.metrics import TASK_STATES

    ids = [f"metrics-depth-{uuid.uuid4().hex[:8]}-{i}" for i in range(3)]
    with SessionLocal() as db:
        db.add(TaskRecord(id=ids[0], type="metrics_probe", state="PENDING"))
        db.add(TaskRecord(id=ids[1], type="metrics_probe", state="PENDING"))
        db.add(TaskRecord(id=ids[2], type="metrics_probe", state="RUNNING"))
        db.commit()

    try:
        def expected() -> dict[str, float]:
            with SessionLocal() as db:
                rows = db.execute(
                    select(TaskRecord.state, func.count()).group_by(TaskRecord.state)).all()
            return {state: float(n) for state, n in rows}

        def exported() -> dict[str, float]:
            body = client.get("/metrics").text
            assert "# TYPE eap_task_queue_depth gauge" in body
            return {l["state"]: v for l, v in _series(body, "eap_task_queue_depth")
                    if "state" in l}

        # M52-D：相等性只对「导出面内的 state」（TASK_STATES）成立——
        # refresh_task_queue_depth 按设计只导出已知 state（显式补零），库里其他用例
        # 残留的非标准 state 行（如 tasks 分页测试的唯一标记）永远不在导出面，
        # 对全库 state 做 == 属误报断言（对导出面内 state 的精确相等语义保持不变）。
        def in_scope(want: dict[str, float]) -> dict[str, float]:
            return {state: n for state, n in want.items() if state in TASK_STATES}

        got: dict[str, float] = {}
        want: dict[str, float] = {}
        for _ in range(20):  # 与引擎后台状态迁移赛跑：最多 ~4s，正常首轮即一致
            want, got = expected(), exported()
            if all(got.get(state, 0.0) == n for state, n in in_scope(want).items()):
                break
            time.sleep(0.2)

        for state, n in in_scope(want).items():
            assert got.get(state, 0.0) == n, f"state={state} 导出值应与 DB 计数一致"
        assert got.get("PENDING", 0.0) >= 2, "插入的 2 个 PENDING 应计入"
        assert got.get("RUNNING", 0.0) >= 1, "插入的 1 个 RUNNING 应计入"
        # 已知 state 显式补零导出（便于仪表盘/告警连续取值）
        for state in ("WAITING_HUMAN", "WAITING_INPUT", "COMPLETED", "FAILED", "CANCELLED"):
            assert state in got, f"已知 state {state} 应显式补零导出"
    finally:
        with SessionLocal() as db:  # 清理探针行，不污染同库其他用例
            db.execute(delete(TaskRecord).where(TaskRecord.id.in_(ids)))
            db.commit()


def test_audit_log_records_admin_ops(client: TestClient, uname):
    """M11 审计：策略创建/启停、任务审批落审计并可查询；敏感字段脱敏。"""
    # 策略创建（含敏感形态 config 验证脱敏）——M52-D：策略名唯一化（脏库重跑不撞唯一约束）
    name = uname("audit-test-policy")
    r = client.post("/api/v1/policies", headers=AUTH, json={
        "name": name, "tenant_id": 0, "kind": "model-allowlist",
        "config": {"models": ["mock-llm"], "api_key": "mask-" + uuid.uuid4().hex[:8]},
    })
    assert r.status_code == 200, r.text
    r2 = client.post(f"/api/v1/policies/{name}/enabled?enabled=false", headers=AUTH)
    assert r2.status_code == 200

    logs = client.get("/api/v1/audit", headers=AUTH).json()
    actions = [l["action"] for l in logs]
    assert "policy.create" in actions and "policy.toggle" in actions
    # 按本用例策略名（审计 target）定位创建日志——脏库残留的其他 policy.create 不干扰
    create_log = next(l for l in logs
                      if l["action"] == "policy.create" and l["target"] == name)
    assert create_log["detail"]["config"]["api_key"] == "***"

    # 按 action 过滤
    filtered = client.get("/api/v1/audit", headers=AUTH, params={"action": "policy.toggle"}).json()
    assert all(l["action"] == "policy.toggle" for l in filtered)

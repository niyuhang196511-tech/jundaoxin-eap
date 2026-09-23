"""在线影子流量测试（M44-A）：配置 CRUD / 抽样分流 / 配对记录 / 报表聚合 / RBAC。

离线确定性：影子调用走注册表内 workflow-as-agent（mock LLM），rate=1.0 恒触发、
rate=0.0 恒跳过；异步影子执行在 TestClient 请求返回后轮询落库。
mock 语义：同输入下主/影输出一致（mock 只回显最后一条 user 消息）——
「影子确实执行了候选 agent」由记录的 shadow_agent 字段与失败用例的真实调用尝试证明。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import AUTH


def _make_workflow(client: TestClient, name: str) -> None:
    """创建 workflow-as-agent（确定性 mock 输出：回显输入）。"""
    dsl = {
        "name": name,
        "version": "1.0.0",
        "steps": [{"id": "gen", "type": "llm", "system": "你是测试助手。"}],
    }
    r = client.post("/api/v1/workflows", headers=AUTH, json=dsl)
    assert r.status_code == 200, r.text


def _create_config(client: TestClient, name: str, source: str, shadow: str,
                   rate: float = 1.0, **kw) -> dict:
    body = {"name": name, "source_agent": source, "shadow_agent": shadow,
            "sample_rate": rate, **kw}
    return client.post("/api/v1/evals/shadow-configs", headers=AUTH, json=body)


def _wait_shadow_runs(client: TestClient, config: str, want: int,
                      timeout_s: float = 8.0) -> list[dict]:
    """轮询影子运行落库（后台任务异步执行）。"""
    deadline = time.time() + timeout_s
    runs: list[dict] = []
    while time.time() < deadline:
        runs = client.get(f"/api/v1/evals/shadow-runs?config={config}", headers=AUTH).json()
        if len(runs) >= want:
            return runs
        time.sleep(0.2)
    return runs


def test_shadow_config_crud_and_rbac(client: TestClient):
    _make_workflow(client, "shadow-src-flow")
    _make_workflow(client, "shadow-cand-flow")

    # 创建：agent 不存在 → 400；重复名 → 409；正常 → 200
    assert _create_config(client, "bad-src", "no-such-agent", "shadow-cand-flow").status_code == 400
    assert _create_config(client, "bad-shadow", "shadow-src-flow", "no-such-agent").status_code == 400
    r = _create_config(client, "crud-cfg", "shadow-src-flow", "shadow-cand-flow", rate=0.5,
                       judge_criteria="回答需正确解决用户问题", note="测试")
    assert r.status_code == 200, r.text
    assert _create_config(client, "crud-cfg", "shadow-src-flow", "shadow-cand-flow").status_code == 409

    # 列表 / 详情字段
    rows = client.get("/api/v1/evals/shadow-configs", headers=AUTH).json()
    row = next(c for c in rows if c["name"] == "crud-cfg")
    assert row["source_agent"] == "shadow-src-flow" and row["shadow_agent"] == "shadow-cand-flow"
    assert row["sample_rate"] == 0.5 and row["judge"] is True

    # PATCH：改抽样率 + 停用
    r = client.patch("/api/v1/evals/shadow-configs/crud-cfg", headers=AUTH,
                     json={"sample_rate": 0.9, "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    # DELETE
    assert client.delete("/api/v1/evals/shadow-configs/crud-cfg", headers=AUTH).status_code == 200
    assert client.delete("/api/v1/evals/shadow-configs/crud-cfg", headers=AUTH).status_code == 404


def test_shadow_traffic_rate_one_couples_pair(client: TestClient):
    """rate=1.0：主调用完成 → 影子全量执行 → 配对落库（输出/延迟齐全）。"""
    _make_workflow(client, "shadow-src1-flow")
    _make_workflow(client, "shadow-cand1-flow")
    assert _create_config(client, "pair-cfg", "shadow-src1-flow", "shadow-cand1-flow",
                          rate=1.0).status_code == 200

    r = client.post("/api/v1/agents/shadow-src1-flow/invocations", headers=AUTH,
                    json={"input": "第一问"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, "pair-cfg", 1)
    assert len(runs) >= 1, "影子运行未落库"
    run = client.get(f"/api/v1/evals/shadow-runs/{runs[0]['id']}", headers=AUTH).json()
    assert run["shadow_ok"] is True, run["shadow_error"]
    assert run["source_agent"] == "shadow-src1-flow"
    assert run["shadow_agent"] == "shadow-cand1-flow"  # 影子确实执行的是候选 agent
    assert "第一问" in run["input"]
    assert "第一问" in run["primary_output"] and "第一问" in run["shadow_output"]
    assert run["primary_latency_ms"] >= 0 and run["shadow_latency_ms"] >= 0
    assert run["trace_id"].endswith("-shadow") is False  # 记录存主 trace（影子 trace 加 -shadow 后缀）

    # 报表聚合：同输入 mock 输出一致 → exact_match_rate=1.0
    report = client.get("/api/v1/evals/shadow-configs/pair-cfg/report", headers=AUTH).json()
    rep = report["report"]
    assert rep["total"] >= 1 and rep["shadow_ok"] >= 1
    assert rep["shadow_fail_rate"] == 0.0
    assert rep["exact_match_rate"] == 1.0
    assert rep["primary_latency_avg_ms"] >= 0 and rep["shadow_latency_avg_ms"] >= 0


def test_shadow_traffic_rate_zero_and_disabled(client: TestClient):
    """rate=0.0 与 enabled=false 均不触发影子。"""
    _make_workflow(client, "shadow-src2-flow")
    _make_workflow(client, "shadow-cand2-flow")
    assert _create_config(client, "zero-cfg", "shadow-src2-flow", "shadow-cand2-flow",
                          rate=0.0).status_code == 200

    r = client.post("/api/v1/agents/shadow-src2-flow/invocations", headers=AUTH,
                    json={"input": "不该触发"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, "zero-cfg", 1, timeout_s=2.0)
    assert runs == [], "rate=0.0 不应产生影子运行"

    # 停用后（rate=1.0）也不触发
    assert _create_config(client, "off-cfg", "shadow-src2-flow", "shadow-cand2-flow",
                          rate=1.0).status_code == 200
    assert client.patch("/api/v1/evals/shadow-configs/off-cfg", headers=AUTH,
                        json={"enabled": False}).status_code == 200
    client.post("/api/v1/agents/shadow-src2-flow/invocations", headers=AUTH,
                json={"input": "停用触发"})
    runs = _wait_shadow_runs(client, "off-cfg", 1, timeout_s=2.0)
    assert runs == [], "停用配置不应产生影子运行"


def test_shadow_failure_recorded_primary_unaffected(client: TestClient):
    """影子 agent 不存在：主调用 200 不受影响，影子失败落 shadow_error。"""
    _make_workflow(client, "shadow-src3-flow")
    assert _create_config(client, "fail-cfg", "shadow-src3-flow", "shadow-src3-flow",
                          rate=1.0).status_code == 200

    # 直插一条引用不存在影子 agent 的配置（绕过创建校验，模拟 agent 注销后配置失效）
    from eap.db import SessionLocal
    from eap.models import ShadowConfigRecord
    db = SessionLocal()
    db.add(ShadowConfigRecord(name="fail-cfg-stale", source_agent="shadow-src3-flow",
                              shadow_agent="no-such-shadow-agent", sample_rate=1.0))
    db.commit()
    db.close()

    r = client.post("/api/v1/agents/shadow-src3-flow/invocations", headers=AUTH,
                    json={"input": "主调用须成功"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, "fail-cfg-stale", 1)
    assert len(runs) >= 1
    assert runs[0]["shadow_ok"] is False
    assert "no-such-shadow-agent" in runs[0]["shadow_error"] or "不存在" in runs[0]["shadow_error"]


def test_shadow_report_judge_requires_criteria(client: TestClient):
    """judge=true 无评分标准 → 400（不静默降级）；空报表 total=0。"""
    _make_workflow(client, "shadow-src4-flow")
    _make_workflow(client, "shadow-cand4-flow")
    assert _create_config(client, "judge-cfg", "shadow-src4-flow", "shadow-cand4-flow",
                          rate=1.0).status_code == 200  # judge_criteria 缺省为空
    r = client.get("/api/v1/evals/shadow-configs/judge-cfg/report", headers=AUTH,
                   params={"judge": "true"})
    assert r.status_code == 400
    assert "judge_criteria" in r.json()["detail"]

    empty = client.get("/api/v1/evals/shadow-configs/judge-cfg/report", headers=AUTH).json()
    assert empty["report"]["total"] == 0

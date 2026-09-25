"""在线影子流量测试（M44-A）：配置 CRUD / 抽样分流 / 配对记录 / 报表聚合 / RBAC。

离线确定性：影子调用走注册表内 workflow-as-agent（mock LLM），rate=1.0 恒触发、
rate=0.0 恒跳过；异步影子执行在 TestClient 请求返回后轮询落库。
mock 语义：同输入下主/影输出一致（mock 只回显最后一条 user 消息）——
「影子确实执行了候选 agent」由记录的 shadow_agent 字段与失败用例的真实调用尝试证明。

M52-D 可重入：工作流/影子配置名一律加模块级 uuid 后缀（脏库重跑不撞
「工作流已存在」/配置名唯一约束；配置名唯一 → shadow-runs/report 按配置过滤
天然只剩本运行数据，精确断言不受残留配对记录干扰）。
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from .conftest import AUTH

_SFX = uuid.uuid4().hex[:8]

SRC = f"shadow-src-flow-{_SFX}"
CAND = f"shadow-cand-flow-{_SFX}"
SRC1 = f"shadow-src1-flow-{_SFX}"
CAND1 = f"shadow-cand1-flow-{_SFX}"
SRC2 = f"shadow-src2-flow-{_SFX}"
CAND2 = f"shadow-cand2-flow-{_SFX}"
SRC3 = f"shadow-src3-flow-{_SFX}"
SRC4 = f"shadow-src4-flow-{_SFX}"
CAND4 = f"shadow-cand4-flow-{_SFX}"
CFG_CRUD = f"crud-cfg-{_SFX}"
CFG_PAIR = f"pair-cfg-{_SFX}"
CFG_ZERO = f"zero-cfg-{_SFX}"
CFG_OFF = f"off-cfg-{_SFX}"
CFG_FAIL = f"fail-cfg-{_SFX}"
CFG_FAIL_STALE = f"fail-cfg-stale-{_SFX}"
CFG_JUDGE = f"judge-cfg-{_SFX}"


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
    _make_workflow(client, SRC)
    _make_workflow(client, CAND)

    # 创建：agent 不存在 → 400（坏配置不落库，固定坏名可保留）；重复名 → 409；正常 → 200
    assert _create_config(client, "bad-src", "no-such-agent", CAND).status_code == 400
    assert _create_config(client, "bad-shadow", SRC, "no-such-agent").status_code == 400
    r = _create_config(client, CFG_CRUD, SRC, CAND, rate=0.5,
                       judge_criteria="回答需正确解决用户问题", note="测试")
    assert r.status_code == 200, r.text
    assert _create_config(client, CFG_CRUD, SRC, CAND).status_code == 409

    # 列表 / 详情字段
    rows = client.get("/api/v1/evals/shadow-configs", headers=AUTH).json()
    row = next(c for c in rows if c["name"] == CFG_CRUD)
    assert row["source_agent"] == SRC and row["shadow_agent"] == CAND
    assert row["sample_rate"] == 0.5 and row["judge"] is True

    # PATCH：改抽样率 + 停用
    r = client.patch(f"/api/v1/evals/shadow-configs/{CFG_CRUD}", headers=AUTH,
                     json={"sample_rate": 0.9, "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    # DELETE
    assert client.delete(f"/api/v1/evals/shadow-configs/{CFG_CRUD}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/v1/evals/shadow-configs/{CFG_CRUD}", headers=AUTH).status_code == 404


def test_shadow_traffic_rate_one_couples_pair(client: TestClient):
    """rate=1.0：主调用完成 → 影子全量执行 → 配对落库（输出/延迟齐全）。"""
    _make_workflow(client, SRC1)
    _make_workflow(client, CAND1)
    assert _create_config(client, CFG_PAIR, SRC1, CAND1, rate=1.0).status_code == 200

    r = client.post(f"/api/v1/agents/{SRC1}/invocations", headers=AUTH,
                    json={"input": "第一问"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, CFG_PAIR, 1)
    assert len(runs) >= 1, "影子运行未落库"
    run = client.get(f"/api/v1/evals/shadow-runs/{runs[0]['id']}", headers=AUTH).json()
    assert run["shadow_ok"] is True, run["shadow_error"]
    assert run["source_agent"] == SRC1
    assert run["shadow_agent"] == CAND1  # 影子确实执行的是候选 agent
    assert "第一问" in run["input"]
    assert "第一问" in run["primary_output"] and "第一问" in run["shadow_output"]
    assert run["primary_latency_ms"] >= 0 and run["shadow_latency_ms"] >= 0
    assert run["trace_id"].endswith("-shadow") is False  # 记录存主 trace（影子 trace 加 -shadow 后缀）

    # 报表聚合：同输入 mock 输出一致 → exact_match_rate=1.0
    report = client.get(f"/api/v1/evals/shadow-configs/{CFG_PAIR}/report", headers=AUTH).json()
    rep = report["report"]
    assert rep["total"] >= 1 and rep["shadow_ok"] >= 1
    assert rep["shadow_fail_rate"] == 0.0
    assert rep["exact_match_rate"] == 1.0
    assert rep["primary_latency_avg_ms"] >= 0 and rep["shadow_latency_avg_ms"] >= 0


def test_shadow_traffic_rate_zero_and_disabled(client: TestClient):
    """rate=0.0 与 enabled=false 均不触发影子。"""
    _make_workflow(client, SRC2)
    _make_workflow(client, CAND2)
    assert _create_config(client, CFG_ZERO, SRC2, CAND2, rate=0.0).status_code == 200

    r = client.post(f"/api/v1/agents/{SRC2}/invocations", headers=AUTH,
                    json={"input": "不该触发"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, CFG_ZERO, 1, timeout_s=2.0)
    assert runs == [], "rate=0.0 不应产生影子运行"

    # 停用后（rate=1.0）也不触发
    assert _create_config(client, CFG_OFF, SRC2, CAND2, rate=1.0).status_code == 200
    assert client.patch(f"/api/v1/evals/shadow-configs/{CFG_OFF}", headers=AUTH,
                        json={"enabled": False}).status_code == 200
    client.post(f"/api/v1/agents/{SRC2}/invocations", headers=AUTH,
                json={"input": "停用触发"})
    runs = _wait_shadow_runs(client, CFG_OFF, 1, timeout_s=2.0)
    assert runs == [], "停用配置不应产生影子运行"


def test_shadow_failure_recorded_primary_unaffected(client: TestClient):
    """影子 agent 不存在：主调用 200 不受影响，影子失败落 shadow_error。"""
    _make_workflow(client, SRC3)
    assert _create_config(client, CFG_FAIL, SRC3, SRC3, rate=1.0).status_code == 200

    # 直插一条引用不存在影子 agent 的配置（绕过创建校验，模拟 agent 注销后配置失效）
    from eap.db import SessionLocal
    from eap.models import ShadowConfigRecord
    db = SessionLocal()
    db.add(ShadowConfigRecord(name=CFG_FAIL_STALE, source_agent=SRC3,
                              shadow_agent="no-such-shadow-agent", sample_rate=1.0))
    db.commit()
    db.close()

    r = client.post(f"/api/v1/agents/{SRC3}/invocations", headers=AUTH,
                    json={"input": "主调用须成功"})
    assert r.status_code == 200
    runs = _wait_shadow_runs(client, CFG_FAIL_STALE, 1)
    assert len(runs) >= 1
    assert runs[0]["shadow_ok"] is False
    assert "no-such-shadow-agent" in runs[0]["shadow_error"] or "不存在" in runs[0]["shadow_error"]


def test_shadow_report_judge_requires_criteria(client: TestClient):
    """judge=true 无评分标准 → 400（不静默降级）；空报表 total=0。"""
    _make_workflow(client, SRC4)
    _make_workflow(client, CAND4)
    assert _create_config(client, CFG_JUDGE, SRC4, CAND4,
                          rate=1.0).status_code == 200  # judge_criteria 缺省为空
    r = client.get(f"/api/v1/evals/shadow-configs/{CFG_JUDGE}/report", headers=AUTH,
                   params={"judge": "true"})
    assert r.status_code == 400
    assert "judge_criteria" in r.json()["detail"]

    empty = client.get(f"/api/v1/evals/shadow-configs/{CFG_JUDGE}/report", headers=AUTH).json()
    assert empty["report"]["total"] == 0

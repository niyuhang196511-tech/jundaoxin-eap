"""M42-B 评测门禁接入模型路由（docs/10 L1 工程部分）：eval-gate 策略 + 模型直评。

- 纯判定逻辑：runtime/policy.py apply_eval_gate（最新记录 verdict/pass_rate、
  无记录 + require_eval、清单外不受影响、无策略原链不变）
- 路由集成：熔断过滤之后接入降级链（gate 剔除落审计 model.eval_gate.blocked，
  链全空抛既有 ProviderError 语义）
- 模型直评：eval.run 带 model 参数（不经 agent），结果落 eval_runs.model 列，
  并作为门禁判定的输入
- 隔离纪律（docs/10 测试约定）：自建模型/策略/评测行在用例结束前停用或删除，
  避免污染 session 级共享测试库；门禁集成模型用 reasoning 能力 + 用后停用双保险
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
CHAT = "/v1/chat/completions"

# 门禁集成用专用模型名（reasoning 能力，不进 chat 链；用后停用防泄漏）
GATED_MODEL = "gate-eval-a"
FALLBACK_MODEL = "gate-eval-b"


# ---------- 纯判定逻辑（apply_eval_gate） ----------


def test_eval_gate_pure_logic(client: TestClient):
    from eap.db import SessionLocal
    from eap.models import EvalRunRecord, ModelRecord, PolicyRecord
    from eap.runtime import policy as policy_mod
    from eap.runtime.policy import apply_eval_gate

    records = [
        ModelRecord(name="gate-a", capabilities=["chat"], provider="mock", priority=1),
        ModelRecord(name="gate-b", capabilities=["chat"], provider="mock", priority=2),
        ModelRecord(name="gate-out", capabilities=["chat"], provider="mock", priority=3),
    ]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _run(model, verdict, pass_rate, offset_s):
        return EvalRunRecord(id=f"m42b-{model}-{offset_s}", agent="", dataset="faq-smoke",
                             model=model, verdict=verdict, kind="agent", judge="rule",
                             min_pass_rate=0.8, pass_rate=pass_rate, scores=[],
                             created_at=now + timedelta(seconds=offset_s))

    # 策略评估按租户上下文（平台内部模式无租户不生效，与 M3 语义一致）——测试内显式设租户 1
    token = policy_mod.set_tenant(1)
    try:
        with SessionLocal() as db:
            # 1) 无 eval-gate 策略：原链原样返回（存量零破坏）
            chain, blocked = apply_eval_gate(db, records)
            assert [r.name for r in chain] == ["gate-a", "gate-b", "gate-out"]
            assert blocked == []

            policy = PolicyRecord(name="m42b-gate-logic", tenant_id=0, kind="eval-gate",
                                  config={"models": ["gate-a", "gate-b"],
                                          "require_eval": True, "min_pass_rate": 0.8})
            db.add(policy)
            db.commit()
            try:
                # 2) 清单内模型无评测记录且 require_eval=true → 剔除；清单外不受影响
                chain, blocked = apply_eval_gate(db, records)
                assert [r.name for r in chain] == ["gate-out"]
                assert {b["model"] for b in blocked} == {"gate-a", "gate-b"}
                assert all("require_eval" in b["reason"] for b in blocked)

                # 3) 清单内模型最新评测 PASS 且通过率达标 → 放行
                db.add(_run("gate-a", "PASS", 0.9, 10))
                db.commit()
                chain, blocked = apply_eval_gate(db, records)
                assert [r.name for r in chain] == ["gate-a", "gate-out"]
                assert [b["model"] for b in blocked] == ["gate-b"]

                # 4) 最新一条 FAIL（旧 PASS 不算数）→ 剔除
                db.add(_run("gate-a", "FAIL", 0.5, 20))
                db.commit()
                chain, blocked = apply_eval_gate(db, records)
                assert [r.name for r in chain] == ["gate-out"]
                assert {b["model"] for b in blocked} == {"gate-a", "gate-b"}
                assert any("非 PASS" in b["reason"] for b in blocked)

                # 5) 最新 PASS 但通过率低于门禁 min_pass_rate → 剔除
                db.add(_run("gate-a", "PASS", 0.6, 30))
                db.commit()
                chain, _blocked = apply_eval_gate(db, records)
                assert [r.name for r in chain] == ["gate-out"]

                # 6) require_eval=false：无评测记录的清单内模型放行
                policy.config = {"models": ["gate-b"], "require_eval": False, "min_pass_rate": 0.8}
                db.commit()
                chain, blocked = apply_eval_gate(db, records)
                assert [r.name for r in chain] == ["gate-a", "gate-b", "gate-out"]
            finally:
                db.execute(delete(EvalRunRecord).where(EvalRunRecord.model.like("gate-%")))
                db.execute(delete(PolicyRecord).where(PolicyRecord.name == "m42b-gate-logic"))
                db.commit()
    finally:
        policy_mod.reset_tenant(token)


# ---------- 路由集成：降级链 + 审计 + 链全空语义 ----------


def _complete(capability: str, prompt: str) -> str:
    """直连 hub 路由链（显式租户 1：策略评估按租户上下文，与 chat 端点行为一致）。"""
    from eap.db import SessionLocal
    from eap.modelhub.router import hub
    from eap.runtime import policy as policy_mod

    async def _run():
        with SessionLocal() as db:
            completion = await hub.complete(db, [{"role": "user", "content": prompt}],
                                            capability=capability)
            return completion.record.name

    token = policy_mod.set_tenant(1)
    try:
        return asyncio.run(_run())
    finally:
        policy_mod.reset_tenant(token)


def test_eval_gate_router_fallback(client: TestClient):
    from eap.db import SessionLocal
    from eap.modelhub.providers import ProviderError
    from eap.models import AuditLog, EvalRunRecord

    for name, prio in ((GATED_MODEL, 1), (FALLBACK_MODEL, 2)):
        resp = client.post("/api/v1/models", headers=HEADERS, json={
            "name": name, "capabilities": ["reasoning"], "provider": "mock", "priority": prio})
        assert resp.status_code == 200, resp.text

    policy = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-route", "kind": "eval-gate",
        "config": {"models": [GATED_MODEL], "require_eval": True, "min_pass_rate": 0.8}}).json()
    try:
        # FAIL 模型被门禁剔除 → 自然落到降级链第二个
        assert _complete("reasoning", "门禁降级测试") == FALLBACK_MODEL

        # 剔除落审计 model.eval_gate.blocked（模型名/原因）
        with SessionLocal() as db:
            rows = db.query(AuditLog).filter(
                AuditLog.action == "model.eval_gate.blocked",
                AuditLog.target == GATED_MODEL).all()
            assert rows, "门禁剔除应落审计"
            assert "require_eval" in rows[-1].detail["reason"]

        # 模型直评写入 PASS 记录（作为门禁输入）→ 门禁放行，直达高优模型
        with SessionLocal() as db:
            db.add(EvalRunRecord(id="m42b-route-pass", agent="", dataset="faq-smoke",
                                 model=GATED_MODEL, verdict="PASS", kind="agent", judge="rule",
                                 min_pass_rate=0.8, pass_rate=1.0, scores=[]))
            db.commit()
        assert _complete("reasoning", "门禁直达测试") == GATED_MODEL

        # 链全空（capability 链上模型均被门禁剔除，含 mock-llm）→ 既有语义错误
        with SessionLocal() as db:
            db.execute(delete(EvalRunRecord).where(EvalRunRecord.model == GATED_MODEL))
            db.commit()
        client.post("/api/v1/policies", headers=HEADERS, json={
            "name": "m42b-gate-route-both", "kind": "eval-gate",
            "config": {"models": [GATED_MODEL, FALLBACK_MODEL, "mock-llm"],
                       "require_eval": True}})
        with pytest.raises(ProviderError) as exc_info:
            _complete("reasoning", "全被门禁剔除")
        assert "评测门禁" in str(exc_info.value)
    finally:
        client.post("/api/v1/policies/m42b-gate-route-both/enabled?enabled=false", headers=HEADERS)
        client.post("/api/v1/policies/m42b-gate-route/enabled?enabled=false", headers=HEADERS)
        for name in (GATED_MODEL, FALLBACK_MODEL):
            client.patch(f"/api/v1/models/{name}?enabled=false", headers=HEADERS)
        with SessionLocal() as db:
            db.execute(delete(EvalRunRecord).where(EvalRunRecord.model.like("gate-eval-%")))
            db.commit()


# ---------- 模型直评（eval.run 带 model） ----------


def _wait_run(client: TestClient, run_id: str) -> dict:
    run: dict = {}
    for _ in range(50):
        run = client.get(f"/api/v1/evals/runs/{run_id}", headers=AUTH).json()
        if run["verdict"] != "PENDING":
            return run
        time.sleep(0.2)
    return run


def test_model_direct_eval(client: TestClient):
    # mock 回声包含用例输入，而 faq-smoke 的 expected_any 关键词均在输入内 → 规则裁判确定性 PASS
    resp = client.post("/api/v1/evals/runs", headers=HEADERS, json={
        "model": "mock-llm", "dataset": "faq-smoke", "min_pass_rate": 0.8})
    assert resp.status_code == 200, resp.text
    run = _wait_run(client, resp.json()["run_id"])
    assert run["verdict"] == "PASS", run.get("scores")
    assert run["model"] == "mock-llm"
    assert run["agent"] == ""
    assert run["pass_rate"] >= 0.8

    # 按模型过滤评测历史
    listed = client.get("/api/v1/evals/runs", params={"model": "mock-llm"}, headers=AUTH).json()
    assert listed and all(r["model"] == "mock-llm" for r in listed)
    assert any(r["run_id"] == run["run_id"] for r in listed)

    # 请求校验：agent 与 model 二选一；模型直评不支持 rag 数据集
    resp = client.post("/api/v1/evals/runs", headers=HEADERS, json={
        "model": "mock-llm", "agent": "faq-agent", "dataset": "faq-smoke"})
    assert resp.status_code == 400
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json={
        "name": "m42b-rag-ds", "kind": "rag",
        "cases": [{"query": "退货政策是什么？", "relevant_chunk_ids": ["chunk-1"]}]}
    ).status_code in (200, 409)
    resp = client.post("/api/v1/evals/runs", headers=HEADERS, json={
        "model": "mock-llm", "dataset": "m42b-rag-ds"})
    assert resp.status_code == 400


def test_eval_gate_policy_validation(client: TestClient):
    # eval-gate 注册 + config 校验
    assert client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-valid", "kind": "eval-gate",
        "config": {"models": ["mock-llm"], "require_eval": True, "min_pass_rate": 0.8}}
    ).status_code == 200
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-bad1", "kind": "eval-gate", "config": {}})
    assert r.status_code == 400 and "config.models" in r.json()["detail"]
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-bad2", "kind": "eval-gate",
        "config": {"models": ["x"], "min_pass_rate": 1.5}})
    assert r.status_code == 400 and "min_pass_rate" in r.json()["detail"]
    client.post("/api/v1/policies/m42b-gate-valid/enabled?enabled=false", headers=HEADERS)

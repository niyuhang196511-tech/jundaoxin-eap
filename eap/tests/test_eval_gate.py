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
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
CHAT = "/v1/chat/completions"

# M52-D 可重入：模块级一次性后缀——模型/策略/评测行 ID 跨运行唯一，脏库重跑不撞约束
_SFX = uuid.uuid4().hex[:8]

# 门禁集成用专用模型名（reasoning 能力，不进 chat 链；用后停用防泄漏）
GATED_MODEL = f"gate-eval-a-{_SFX}"
FALLBACK_MODEL = f"gate-eval-b-{_SFX}"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_gate_policies(client: TestClient):
    """模块结束后删除本模块新增的策略行（差量清理，样板=test_tool_governance）：
    eval-gate 残留启用策略会污染后续文件的模型路由断言。"""
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
        return EvalRunRecord(id=f"m42b-{_SFX}-{model}-{offset_s}", agent="", dataset="faq-smoke",
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

            policy = PolicyRecord(name=f"m42b-gate-logic-{_SFX}", tenant_id=0, kind="eval-gate",
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
                db.execute(delete(PolicyRecord)
                           .where(PolicyRecord.name == f"m42b-gate-logic-{_SFX}"))
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

    client.post("/api/v1/policies", headers=HEADERS, json={
        "name": f"m42b-gate-route-{_SFX}", "kind": "eval-gate",
        "config": {"models": [GATED_MODEL], "require_eval": True, "min_pass_rate": 0.8}})
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
            db.add(EvalRunRecord(id=f"m42b-route-pass-{_SFX}", agent="", dataset="faq-smoke",
                                 model=GATED_MODEL, verdict="PASS", kind="agent", judge="rule",
                                 min_pass_rate=0.8, pass_rate=1.0, scores=[]))
            db.commit()
        assert _complete("reasoning", "门禁直达测试") == GATED_MODEL

        # 链全空（capability 链上模型均被门禁剔除，含 mock-llm）→ 既有语义错误
        with SessionLocal() as db:
            db.execute(delete(EvalRunRecord).where(EvalRunRecord.model == GATED_MODEL))
            # M52-D：mock-llm 为 reasoning 链尾（种子能力含 reasoning）——上一遍运行
            # 本文件 test_model_direct_eval 留下的 PASS 记录会让门禁放行 mock-llm，
            # 「链全空」前提失效。该记录源于本文件，按本文件隔离纪律（自建评测行
            # 用后删除）在此清掉；后跑的 test_model_direct_eval 每次自建新记录不受影响。
            db.execute(delete(EvalRunRecord).where(EvalRunRecord.model == "mock-llm"))
            db.commit()
        client.post("/api/v1/policies", headers=HEADERS, json={
            "name": f"m42b-gate-route-both-{_SFX}", "kind": "eval-gate",
            "config": {"models": [GATED_MODEL, FALLBACK_MODEL, "mock-llm"],
                       "require_eval": True}})
        with pytest.raises(ProviderError) as exc_info:
            _complete("reasoning", "全被门禁剔除")
        assert "评测门禁" in str(exc_info.value)
    finally:
        client.post(f"/api/v1/policies/m42b-gate-route-both-{_SFX}/enabled?enabled=false",
                    headers=HEADERS)
        client.post(f"/api/v1/policies/m42b-gate-route-{_SFX}/enabled?enabled=false",
                    headers=HEADERS)
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
        "name": f"m42b-gate-valid-{_SFX}", "kind": "eval-gate",
        "config": {"models": ["mock-llm"], "require_eval": True, "min_pass_rate": 0.8}}
    ).status_code == 200
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-bad1", "kind": "eval-gate", "config": {}})
    assert r.status_code == 400 and "config.models" in r.json()["detail"]
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "m42b-gate-bad2", "kind": "eval-gate",
        "config": {"models": ["x"], "min_pass_rate": 1.5}})
    assert r.status_code == 400 and "min_pass_rate" in r.json()["detail"]
    client.post(f"/api/v1/policies/m42b-gate-valid-{_SFX}/enabled?enabled=false",
                headers=HEADERS)


def test_model_direct_eval_no_fallback(client: TestClient):
    """M45-B 直评直连（only_prefer）：目标模型供应商失败 → 逐用例落 error 判 FAIL，
    绝不落到链上其他模型应答（对照：普通调用同场景会降级到 mock-llm）。"""
    # 注册必然失败的高优先级 openai_compat 模型（不可路由地址，test_hub 同款手法）
    dead_model = f"eval-dead-model-{_SFX}"
    resp = client.post("/api/v1/models", headers=HEADERS, json={
        "name": dead_model, "capabilities": ["chat"],
        "provider": "openai_compat", "base_url": "http://127.0.0.1:9",
        "api_key": "x", "remote_model": "x", "priority": 1})
    assert resp.status_code == 200, resp.text

    try:
        # 对照组（钉死参数之前的行为基准）：普通 chat 调用沿链降级到 mock-llm
        chat = client.post(CHAT, headers=HEADERS,
                           json={"messages": [{"role": "user", "content": "直连对照"}]})
        assert chat.status_code == 200
        assert chat.json()["model"] == "mock-llm"

        # 直评 dead-model：only_prefer 钉死 → 每个用例 ProviderError 落 error → FAIL
        resp = client.post("/api/v1/evals/runs", headers=HEADERS, json={
            "model": dead_model, "dataset": "faq-smoke", "min_pass_rate": 0.8})
        assert resp.status_code == 200, resp.text
        run = _wait_run(client, resp.json()["run_id"])
        assert run["verdict"] == "FAIL", run.get("scores")
        assert run["model"] == dead_model
        assert run["pass_rate"] == 0.0
        assert run["scores"], "应有逐用例记录"
        for case in run["scores"]:
            assert "error" in case, f"用例应带 error 而非降级应答：{case}"
            assert "output_snippet" not in case, "不应出现其他模型（mock-llm）的应答"
    finally:
        # 隔离纪律：用后停用，防污染 session 级共享测试库的路由链
        client.post(f"/api/v1/models/{dead_model}/enabled?enabled=false", headers=HEADERS)


def test_model_direct_eval_pinned_missing_model(client: TestClient):
    """only_prefer 直连不存在的模型 → 评测任务失败（ProviderError 语义），不静默换模型。"""
    resp = client.post("/api/v1/evals/runs", headers=HEADERS, json={
        "model": "no-such-model-x", "dataset": "faq-smoke"})
    assert resp.status_code == 200
    run = _wait_run(client, resp.json()["run_id"])
    assert run["verdict"] == "FAIL"
    for case in run["scores"]:
        assert "error" in case
        assert "no-such-model-x" in case["error"]

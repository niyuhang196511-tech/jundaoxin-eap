"""成本中心：预算设置 / 用量汇总 / 超限熔断（docs/08 §4）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
INVOKE = "/api/v1/agents/faq-agent/invocations"


def test_budget_summary_and_circuit_breaker(client):
    # 未设预算：不熔断，正常调用并产生计量
    r = client.post(INVOKE, headers=HEADERS, json={"input": "如何创建知识库？"})
    assert r.status_code == 200

    summary = client.get("/api/v1/budgets/1/summary", headers=HEADERS).json()
    assert summary["tenant_id"] == 1 and summary["calls"] >= 1
    assert summary["tokens_total"] > 0
    assert any(k in ("agent", "chat") for k in summary["by_kind"])

    # 设置极小预算（当月已用量必然超过）→ 下一跳调用被熔断 429
    used = summary["tokens_total"]
    r = client.put("/api/v1/budgets", headers=HEADERS,
                   json={"tenant_id": 1, "monthly_token_budget": used})
    assert r.status_code == 200 and r.json()["monthly_token_budget"] == used
    r = client.post(INVOKE, headers=HEADERS, json={"input": "再问一句"})
    assert r.status_code == 429 and "EAP-7001" in r.json()["detail"]

    # 预算详情：blocked=True；提升预算后恢复放行
    detail = client.get("/api/v1/budgets/1", headers=HEADERS).json()
    assert detail["blocked"] is True and detail["monthly_token_budget"] == used
    r = client.put("/api/v1/budgets", headers=HEADERS,
                   json={"tenant_id": 1, "monthly_token_budget": used * 10})
    assert r.status_code == 200
    r = client.post(INVOKE, headers=HEADERS, json={"input": "预算提升后再问"})
    assert r.status_code == 200

    # 禁用预算 → 不再熔断
    client.put("/api/v1/budgets", headers=HEADERS,
               json={"tenant_id": 1, "monthly_token_budget": 1, "enabled": False})
    r = client.post(INVOKE, headers=HEADERS, json={"input": "禁用预算后照常调用"})
    assert r.status_code == 200

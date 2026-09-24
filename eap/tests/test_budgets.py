"""成本中心：预算设置 / 用量汇总 / 超限熔断（docs/08 §4）。
成本报表导出（M48-A）：GET /api/v1/budgets/report/export（admin，CSV 附件，
复用 report 聚合与过滤参数，导出动作落审计 budget.export 仅条数）。
"""

from __future__ import annotations

import csv as csv_mod
import io
import time

from .conftest import AUTH
from .test_oidc import fake_idp, _make_id_token  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}
INVOKE = "/api/v1/agents/faq-agent/invocations"

ISSUER = "https://idp.example"


def _token(roles: list[str]) -> str:
    claims = {"iss": ISSUER, "sub": "budget@corp", "exp": int(time.time()) + 600,
              "tenant_id": 1, "roles": roles}
    return _make_id_token(claims)


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


# ---------- 成本报表导出（M48-A） ----------


def test_report_export_csv_shape(client):
    """CSV 导出：附件下载头 + UTF-8 BOM + 表头列序 + 四段 section + total 与报表总成本一致。

    导出端点为固定路径 GET /api/v1/budgets/report/export?tenant_id=&days=
    （避开 /{tenant_id} 通配），过滤参数与 report 查询一致；mock 模型配价默认 0
    （cost 断言只对齐报表口径，不要求 > 0）。
    """
    r = client.post(INVOKE, headers=HEADERS, json={"input": "报表导出前先产生计量"})
    assert r.status_code == 200
    report = client.get("/api/v1/budgets/1/report", headers=HEADERS, params={"days": 30}).json()
    assert report["tenant_id"] == 1 and report["by_model"]

    resp = client.get("/api/v1/budgets/report/export", headers=HEADERS,
                      params={"tenant_id": 1, "days": 30})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    dispo = resp.headers["content-disposition"]
    assert dispo.startswith("attachment") and "budget-report-tenant1-" in dispo \
        and dispo.endswith('.csv"')
    body = resp.content.decode("utf-8")
    assert body.startswith("\ufeff")  # BOM：Excel 直开中文不乱码
    rows = list(csv_mod.DictReader(io.StringIO(body.lstrip("\ufeff"))))
    assert rows, "至少导出 total 一行"
    assert list(rows[0].keys()) == ["section", "dimension", "calls",
                                    "tokens_in", "tokens_out", "cost"]
    sections = {r["section"] for r in rows}
    assert {"model", "agent", "day", "total"} <= sections  # 复用 report 三维聚合 + 汇总行
    # model 行的调用数、token 与报表一致
    model_rows = {r["dimension"]: r for r in rows if r["section"] == "model"}
    for m in report["by_model"]:
        assert int(model_rows[m["model"]]["calls"]) == m["calls"]
        assert int(model_rows[m["model"]]["tokens_out"]) == m["tokens_out"]
    total = next(r for r in rows if r["section"] == "total")
    assert abs(float(total["cost"]) - report["total_cost"]) < 1e-9
    # 过滤参数生效：无用量租户 → 仅 total 兜底行且成本 0
    resp = client.get("/api/v1/budgets/report/export", headers=HEADERS,
                      params={"tenant_id": 999, "days": 365})
    assert resp.status_code == 200
    rows999 = list(csv_mod.DictReader(
        io.StringIO(resp.content.decode("utf-8").lstrip("\ufeff"))))
    assert [r["section"] for r in rows999] == ["total"]
    assert float(rows999[0]["cost"]) == 0.0


def test_report_export_member_403(client, fake_idp):  # noqa: F811
    """导出为 admin 专用：JWT member 角色 → 403（对齐 triggers/lora RBAC）。"""
    member = _token(roles=["member"])
    resp = client.get("/api/v1/budgets/report/export",
                      params={"tenant_id": 1},
                      headers={"Authorization": f"Bearer {member}"})
    assert resp.status_code == 403, resp.text


def test_report_export_audits(client):
    """导出动作自身落审计 budget.export：仅条数与过滤条件（days/rows），不含导出内容。"""
    client.post(INVOKE, headers=HEADERS, json={"input": "审计留痕导出前调用"})
    resp = client.get("/api/v1/budgets/report/export", headers=HEADERS,
                      params={"tenant_id": 1, "days": 7})
    assert resp.status_code == 200
    entries = client.get("/api/v1/audit", headers=AUTH,
                         params={"action": "budget.export", "target": "1", "limit": 5}).json()
    assert entries and entries[0]["action"] == "budget.export", entries
    detail = entries[0]["detail"]
    assert set(detail) == {"days", "rows"}, detail  # 仅条数与过滤条件
    assert detail["days"] == 7 and detail["rows"] >= 1

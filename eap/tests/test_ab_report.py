"""L10/M34：A/B 实验效果报表与结论辅助（tests 离线确定性）。

覆盖：变体归因埋点（prompt.render 审计 + agent.run.completed 携带 prompt_variant）、
报表分 variant 聚合（渲染占比 vs percent_b 配置、归因调用/成功率/token/延迟）、
conclude（禁用 + 可选 promote + 审计）、RBAC 非 admin 403、无实验 404。

归因口径局限（与报表 attribution 字段一致）：调用指标为 trace_id/prompt_variant
联合近似，仅覆盖同步智能体调用内部渲染了实验 prompt 的调用。
"""

from __future__ import annotations

import hashlib

from .conftest import AUTH
from .test_jwt_auth import _access_token  # noqa: F401 复用 JWT 构造
from .test_oidc import fake_idp  # noqa: F401,F811 复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}
V1 = "请作为企业官网客服，用不超过三句话回答用户问题，语气友好。用户问题：{{question}}。请仅依据给定资料回答并标注 [n] 引用。"
V2 = "请作为资深客服专家，用结构化要点回答用户问题。用户问题：{{question}}。引用资料需标注 [n]。"


def _bucket(prompt_name: str, key: str, percent_b: int = 50) -> str:
    """复刻 runtime.prompts.resolve_template 的分流算法（离线确定性选 key）。"""
    digest = hashlib.sha256(f"prompt:{prompt_name}:{key}".encode()).hexdigest()
    return "b" if int(digest[:8], 16) % 100 < percent_b else "a"


def _keys_for_both_buckets(prompt_name: str, percent_b: int = 50) -> tuple[str, str]:
    key_a = key_b = None
    for i in range(5000):
        key = f"u-{i}"
        if _bucket(prompt_name, key, percent_b) == "b":
            key_b = key_b or key
        else:
            key_a = key_a or key
        if key_a and key_b:
            return key_a, key_b
    raise AssertionError("5000 个 key 未覆盖两个分流桶")


def _render(client, name: str, key: str) -> dict:
    r = client.post(f"/api/v1/prompts/{name}/render", headers=HEADERS,
                    json={"variables": {"question": "q"}, "key": key})
    assert r.status_code == 200, r.text
    return r.json()


def test_ab_report_split_counts(client):
    """渲染占比：A/B 两桶各渲染 N 次 → 报表两 variant 计数精确、占比 vs percent_b 配置。"""
    name = "ab-report-split"
    r = client.post("/api/v1/prompts", headers=HEADERS,
                    json={"name": name, "version": "1.0.0", "template": V1})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/v1/prompts/{name}/versions", headers=HEADERS,
                       json={"version": "2.0.0", "template": V2}).status_code == 200
    assert client.post("/api/v1/prompts/experiments", headers=HEADERS,
                       json={"name": "exp-report-split", "prompt": name,
                             "version_a": "1.0.0", "version_b": "2.0.0",
                             "percent_b": 50}).status_code == 200

    key_a, key_b = _keys_for_both_buckets(name)
    for _ in range(3):
        assert _render(client, name, key_a)["experiment"]["picked"] == "a"
    for _ in range(2):
        assert _render(client, name, key_b)["experiment"]["picked"] == "b"

    r = client.get("/api/v1/prompts/experiments/exp-report-split/report", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variants"]["a"]["renders"] == 3
    assert body["variants"]["b"]["renders"] == 2
    assert body["variants"]["fallback"]["renders"] == 0
    assert body["split"]["classified_renders"] == 5
    assert body["split"]["observed_b_share"] == 40.0
    assert body["split"]["percent_b_config"] == 50
    assert body["split"]["deviation"] == -10.0
    # 无归因调用 → 0/null，不编造
    assert body["variants"]["a"]["attributed_invocations"] == 0
    assert body["variants"]["a"]["success"]["rate"] is None
    assert body["variants"]["b"]["latency_ms"]["p50"] is None
    assert isinstance(body["attribution"], dict) and body["attribution"]["renders"]

    # 时间窗：since_hours=0 → 窗口起点为当前时刻，先前的渲染全部排除
    r = client.get("/api/v1/prompts/experiments/exp-report-split/report",
                   headers={**HEADERS}, params={"since_hours": 0})
    assert r.status_code == 200 and r.json()["variants"]["a"]["renders"] == 0
    r = client.get("/api/v1/prompts/experiments/exp-report-split/report",
                   headers=HEADERS, params={"since_hours": 9999})
    assert r.status_code == 200 and r.json()["variants"]["a"]["renders"] == 3


def test_ab_report_invocation_attribution(client):
    """调用归因：faq-agent 调用内部渲染 faq-answer-style（percent_b=100 全 B）→
    报表 b variant 归因到调用次数/成功率/token/延迟。"""
    assert client.post("/api/v1/prompts/faq-answer-style/versions", headers=HEADERS,
                       json={"version": "2.0.0", "template": V2}).status_code == 200
    assert client.post("/api/v1/prompts/experiments", headers=HEADERS,
                       json={"name": "exp-faq-rpt", "prompt": "faq-answer-style",
                             "version_a": "1.0.0", "version_b": "2.0.0",
                             "percent_b": 100}).status_code == 200
    r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                    json={"input": "如何创建知识库？", "session_id": "s-ab-rpt"})
    assert r.status_code == 200 and r.json()["trace_id"], r.text

    body = client.get("/api/v1/prompts/experiments/exp-faq-rpt/report",
                      headers=HEADERS).json()
    b = body["variants"]["b"]
    assert b["renders"] >= 1
    assert b["attributed_invocations"] >= 1
    assert b["success"]["ok"] >= 1 and b["success"]["rate"] == 1.0
    assert b["tokens_in"] >= 0 and b["tokens_out"] >= 0 and b["cost"] >= 0
    assert b["latency_ms"]["p50"] is not None and b["latency_ms"]["mean"] is not None
    # A variant（1.0.0）无归因调用
    assert body["variants"]["a"]["attributed_invocations"] == 0


def test_conclude_disables_and_promotes(client):
    """conclude：winner=b + promote → 实验禁用、胜出版本发布为当前版、审计落库。"""
    body = client.post("/api/v1/prompts/experiments/exp-faq-rpt/conclude", headers=HEADERS,
                       json={"winner": "b", "reason": "B 桶回答结构化更优", "promote": True})
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["enabled"] is False and data["promoted"] is True
    assert data["version"] == "2.0.0" and data["state"] == "concluded"
    # 发布指针已切到胜出版本；实验禁用后渲染走当前发布版
    assert client.post("/api/v1/prompts/faq-answer-style/render", headers=HEADERS,
                       json={"variables": {"question": "q"}}).json()["version"] == "2.0.0"
    assert client.post("/api/v1/prompts/faq-answer-style/render", headers=HEADERS,
                       json={"variables": {"question": "q"}, "key": "k-after"}).json()["experiment"] is None
    # 审计：prompt.ab.conclude（含 winner/reason/promoted）
    audit_rows = client.get("/api/v1/audit", headers=HEADERS,
                            params={"action": "prompt.ab.conclude"}).json()
    assert any(row["target"] == "exp-faq-rpt"
               and row["detail"]["winner"] == "b"
               and row["detail"]["promoted"] is True
               and "结构化" in row["detail"]["reason"] for row in audit_rows)


def test_report_and_conclude_unknown_experiment_404(client):
    assert client.get("/api/v1/prompts/experiments/no-such-exp/report",
                      headers=HEADERS).status_code == 404
    assert client.post("/api/v1/prompts/experiments/no-such-exp/conclude", headers=HEADERS,
                       json={"winner": "a"}).status_code == 404


def test_ab_report_rbac_member_403(client, fake_idp):  # noqa: F811 参数仅为激活夹具（模块级导入供 pytest 发现）
    """RBAC：member JWT 访问报表/结论端点 → 403（admin 语义，与审计查询一致）。"""
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    assert client.get("/api/v1/prompts/experiments/exp-faq-rpt/report",
                      headers=member).status_code == 403
    assert client.post("/api/v1/prompts/experiments/exp-faq-rpt/conclude", headers=member,
                       json={"winner": "a"}).status_code == 403
    # admin JWT 放行
    admin = {"Authorization": f"Bearer {_access_token(roles=['admin'])}"}
    assert client.get("/api/v1/prompts/experiments/exp-faq-rpt/report",
                      headers=admin).status_code == 200


def test_conclude_disabled_experiment_and_report_after(client):
    """非 promote 的 conclude 仅禁用实验；再 promote 走版本流水线发布胜出版本；
    报表在 conclude 后仍可查（disabled 状态如实返回）。"""
    name = "ab-report-badver"
    assert client.post("/api/v1/prompts", headers=HEADERS,
                       json={"name": name, "version": "1.0.0", "template": V1}).status_code == 200
    # version_b 未创建 → 建实验本身 404（版本必须先存在）
    assert client.post("/api/v1/prompts/experiments", headers=HEADERS,
                       json={"name": "exp-report-badver", "prompt": name,
                             "version_a": "1.0.0", "version_b": "2.0.0",
                             "percent_b": 50}).status_code == 404
    # A/B 同指 1.0.0 建实验：先仅禁用（promote=False 不动发布指针）
    assert client.post("/api/v1/prompts/experiments", headers=HEADERS,
                       json={"name": "exp-report-badver", "prompt": name,
                             "version_a": "1.0.0", "version_b": "1.0.0",
                             "percent_b": 50}).status_code == 200
    r = client.post("/api/v1/prompts/experiments/exp-report-badver/conclude", headers=HEADERS,
                    json={"winner": "a", "reason": "仅禁用", "promote": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["promoted"] is False
    assert client.post(f"/api/v1/prompts/{name}/render", headers=HEADERS,
                       json={"variables": {"question": "q"}}).json()["version"] == "1.0.0"
    # 再 conclude（幂等）：promote=true 走流水线重新发布 1.0.0 → 仍 200
    r = client.post("/api/v1/prompts/experiments/exp-report-badver/conclude", headers=HEADERS,
                    json={"winner": "b", "promote": True})
    assert r.status_code == 200 and r.json()["promoted"] is True and r.json()["version"] == "1.0.0"
    # conclude 后报表仍可查：enabled=false 如实返回
    report = client.get("/api/v1/prompts/experiments/exp-report-badver/report",
                        headers=HEADERS)
    assert report.status_code == 200 and report.json()["enabled"] is False

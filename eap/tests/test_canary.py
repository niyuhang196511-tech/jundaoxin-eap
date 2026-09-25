"""Canary 灰度：review→canary→prod 生命周期、按 user/session 稳定分流、overrides 生效（docs/06 §2）。

M52-D 可重入：门禁数据集名/发布版本号加模块级 uuid 后缀（ReleaseCreate.version
pattern 允许 "-" 后缀，样板=test_releases）——脏库重跑不撞「数据集名 /
faq-agent@version 已存在」唯一约束；canary 定位改为按本运行创建的版本号精确取值
（不再全表 next(state==canary)，残留发布不会误入）。
"""

from __future__ import annotations

import uuid

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
_SFX = uuid.uuid4().hex[:8]
V_DRAFT = f"0.9.0-{_SFX}"
V_P0 = f"10.0.0-{_SFX}"
V_P100 = f"10.1.0-{_SFX}"
V_P50 = f"10.2.0-{_SFX}"
V_ROLLBACK = f"10.3.0-{_SFX}"
DATASET = {
    "name": f"canary-gate-ds-{_SFX}",
    "cases": [{"input": "如何创建知识库？", "expected_any": ["知识库", "kb"]}],
}


def _make_canary(client, version, percent, overrides=None):
    """登记 → 门禁评测 PASS → 进入 canary 并设比例。"""
    r = client.post("/api/v1/releases", headers=HEADERS,
                    json={"agent": "faq-agent", "version": version})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS)  # → review
    r = client.post(f"/api/v1/releases/{rid}/eval", headers=HEADERS, json={"dataset": DATASET["name"]})
    assert r.json()["eval_verdict"] == "PASS"
    r = client.post(f"/api/v1/releases/{rid}/canary", headers=HEADERS,
                    json={"percent": percent, "overrides": overrides or {"model": "mock-llm"}})
    assert r.json()["state"] == "canary" and r.json()["canary_percent"] == percent
    return rid


def test_canary_lifecycle_and_split(client):
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json=DATASET).status_code == 200

    # 未过门禁不能进 canary：draft 直接尝试 → 403
    r = client.post("/api/v1/releases", headers=HEADERS,
                    json={"agent": "faq-agent", "version": V_DRAFT})
    assert r.status_code == 200, r.text
    draft_id = r.json()["id"]
    client.post(f"/api/v1/releases/{draft_id}/promote", headers=HEADERS)  # → review
    r = client.post(f"/api/v1/releases/{draft_id}/canary", headers=HEADERS,
                    json={"percent": 50})
    assert r.status_code == 403 and "EAP-6001" in r.json()["detail"]

    # percent=0：不灰度
    _make_canary(client, V_P0, 0)
    r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                    json={"input": "你好", "user_id": "u-a"})
    assert r.json()["canary"] is None

    # percent=100：全量进 canary，响应带灰度标记，overrides.model 生效（mock-llm）
    canary_id = _make_canary(client, V_P100, 100, {"model": "mock-llm"})
    r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                    json={"input": "你好", "user_id": "u-a"})
    body = r.json()
    assert body["canary"] and body["canary"]["version"] == V_P100
    assert body["canary"]["percent"] == 100
    assert body["usage"].get("model") == "mock-llm"

    # 稳定性：同一 user 多次调用结果一致（canary 或非 canary，不抖动）
    marks = [client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                         json={"input": "你好", "user_id": "u-stable"}).json()["canary"]
             is not None for _ in range(3)]
    assert len(set(marks)) == 1

    # percent=50：两个固定 user 落入不同桶或至少按 hash 确定；比例可调整
    _make_canary(client, V_P50, 50)
    seen = set()
    for i in range(20):
        r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                        json={"input": "你好", "user_id": f"split-{i}"})
        seen.add(r.json()["canary"] is not None)
    assert seen == {True, False}, "50% 比例下应同时出现两个桶"

    # canary→prod：灰度标记消失，state=prod
    r = client.post(f"/api/v1/releases/{canary_id}/promote", headers=HEADERS)
    assert r.json()["state"] == "prod" and r.json()["canary_percent"] == 0
    r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                    json={"input": "你好", "user_id": "u-a"})
    assert r.json()["canary"] is None  # 10.1.0 已被退役，无 canary 发布

    # canary 可直接回滚下线（列表端点按本运行版本号取值并核实 canary 态）
    _make_canary(client, V_ROLLBACK, 100)
    versions = {rel["version"]: rel for rel in
                client.get("/api/v1/releases", headers=HEADERS, params={"agent": "faq-agent"}).json()}
    canary2 = versions[V_ROLLBACK]
    assert canary2["state"] == "canary"
    r = client.post(f"/api/v1/releases/{canary2['id']}/rollback", headers=HEADERS)
    assert r.json()["rolled_back"]["state"] == "rolled_back"

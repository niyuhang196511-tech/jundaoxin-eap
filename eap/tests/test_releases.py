"""发布治理：生命周期状态机 + 评测门禁 + 回滚（docs/06 §2）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
DATASET = {
    "name": "release-gate-ds",
    "cases": [
        {"input": "如何创建知识库？", "expected_any": ["知识库", "kb"]},
        {"input": "如何注册手写智能体？", "expected_any": ["register_agent", "智能体", "注册"]},
    ],
}


def _create_release(client, agent="faq-agent", version="1.0.0"):
    resp = client.post("/api/v1/releases", headers=HEADERS,
                       json={"agent": agent, "version": version, "notes": "测试发布"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_release_lifecycle_with_gate(client):
    # 门禁数据集（faq-agent 的真实回答包含知识库相关关键词）
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json=DATASET).status_code == 200

    # 未注册 agent 拒绝登记
    r = client.post("/api/v1/releases", headers=HEADERS,
                    json={"agent": "no-such-agent", "version": "0.0.1"})
    assert r.status_code == 404

    # 登记草案 → 重复版本 409
    rel = _create_release(client, version="2.0.0")
    assert rel["state"] == "draft"
    assert client.post("/api/v1/releases", headers=HEADERS,
                       json={"agent": "faq-agent", "version": "2.0.0"}).status_code == 409

    # 未评测直接提升到 prod → 403 门禁拦截
    rid = rel["id"]
    assert client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS).status_code == 200  # → review
    r = client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS)
    assert r.status_code == 403 and "EAP-6001" in r.json()["detail"]

    # FAIL 评测也不能放行
    bad_ds = {**DATASET, "name": "release-gate-bad",
              "cases": [{"input": "随便问点什么", "expected_any": ["绝不出现的关键词"]}],
              "min_pass_rate": 1.0}
    assert client.post("/api/v1/evals/datasets", headers=HEADERS, json=bad_ds).status_code == 200
    r = client.post(f"/api/v1/releases/{rid}/eval", headers=HEADERS,
                    json={"dataset": "release-gate-bad", "min_pass_rate": 1.0})
    assert r.json()["eval_verdict"] == "FAIL"
    assert client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS).status_code == 403

    # PASS 评测 → 提升 prod；成为当前 prod
    r = client.post(f"/api/v1/releases/{rid}/eval", headers=HEADERS, json={"dataset": DATASET["name"]})
    assert r.json()["eval_verdict"] == "PASS"
    r = client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS)
    assert r.json()["state"] == "prod"
    r = client.get("/api/v1/releases/current/faq-agent", headers=HEADERS)
    assert r.json()["id"] == rid

    # 已 prod 再提升/评测 → 409
    assert client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS).status_code == 409
    assert client.post(f"/api/v1/releases/{rid}/eval", headers=HEADERS,
                       json={"dataset": DATASET["name"]}).status_code == 409


def test_promote_replaces_old_prod_and_rollback_restores(client):
    # 测试间共享 session 客户端：数据集可能已由前一个用例创建
    ds = client.post("/api/v1/evals/datasets", headers=HEADERS, json=DATASET)
    assert ds.status_code in (200, 409)

    v1 = _create_release(client, version="3.0.0")
    v2 = _create_release(client, version="3.1.0")
    for rel in (v1, v2):
        rid = rel["id"]
        client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS)  # → review
        client.post(f"/api/v1/releases/{rid}/eval", headers=HEADERS, json={"dataset": DATASET["name"]})
        client.post(f"/api/v1/releases/{rid}/promote", headers=HEADERS)  # → prod

    # 后发布者占据 prod，旧版退役
    assert client.get("/api/v1/releases/current/faq-agent", headers=HEADERS).json()["id"] == v2["id"]
    states = {r["id"]: r["state"]
              for r in client.get("/api/v1/releases", headers=HEADERS,
                                  params={"agent": "faq-agent"}).json()}
    assert states[v1["id"]] == "retired" and states[v2["id"]] == "prod"

    # 回滚 v2 → v1 自动恢复为 prod
    r = client.post(f"/api/v1/releases/{v2['id']}/rollback", headers=HEADERS)
    body = r.json()
    assert body["rolled_back"]["state"] == "rolled_back"
    assert body["restored"]["id"] == v1["id"]
    assert client.get("/api/v1/releases/current/faq-agent", headers=HEADERS).json()["id"] == v1["id"]

    # 非 prod 状态不可回滚
    assert client.post(f"/api/v1/releases/{v2['id']}/rollback", headers=HEADERS).status_code == 409

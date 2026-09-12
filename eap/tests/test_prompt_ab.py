"""Prompt 版本流水线与 A/B 实验（docs/05 §3）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
V1 = "请作为企业官网客服，用不超过三句话回答用户问题，语气友好。用户问题：{{question}}。请仅依据给定资料回答并标注 [n] 引用。"
V2 = "请作为资深客服专家，用结构化要点回答用户问题。用户问题：{{question}}。引用资料需标注 [n]。"


def _make_prompt(client, name="style-ab"):
    r = client.post("/api/v1/prompts", headers=HEADERS,
                    json={"name": name, "version": "1.0.0", "template": V1})
    assert r.status_code == 200, r.text
    return name


def test_version_pipeline_publish_rollback(client):
    _make_prompt(client)
    # 当前发布 = v1；创建 v2 草稿 → 重复版本 409
    assert client.post("/api/v1/prompts/style-ab/render", headers=HEADERS,
                       json={"variables": {"question": "q"}}).json()["version"] == "1.0.0"
    r = client.post("/api/v1/prompts/style-ab/versions", headers=HEADERS,
                    json={"version": "1.1.0", "template": V2, "notes": "结构化改版"})
    assert r.status_code == 200 and r.json()["state"] == "draft"
    assert client.post("/api/v1/prompts/style-ab/versions", headers=HEADERS,
                       json={"version": "1.1.0", "template": V2}).status_code == 409

    # 未发布的 v2 不影响线上；发布校验：缺变量样例 → 400
    r = client.post("/api/v1/prompts/style-ab/publish", headers=HEADERS,
                    json={"version": "1.1.0"})
    assert r.status_code == 400 and "EAP-4000" in r.json()["detail"]
    r = client.post("/api/v1/prompts/style-ab/publish", headers=HEADERS,
                    json={"version": "1.1.0", "variables_sample": {"question": "样例"}})
    assert r.status_code == 200 and r.json()["state"] == "published"
    assert client.post("/api/v1/prompts/style-ab/render", headers=HEADERS,
                       json={"variables": {"question": "q"}}).json()["version"] == "1.1.0"

    # 版本历史：v2 published、v1 archived
    versions = {v["version"]: v["state"]
                for v in client.get("/api/v1/prompts/style-ab/versions", headers=HEADERS).json()}
    assert versions == {"1.0.0": "archived", "1.1.0": "published"}

    # 回滚 → v1 恢复为发布版
    r = client.post("/api/v1/prompts/style-ab/rollback", headers=HEADERS)
    assert r.json()["version"] == "1.0.0"
    assert client.post("/api/v1/prompts/style-ab/render", headers=HEADERS,
                       json={"variables": {"question": "q"}}).json()["version"] == "1.0.0"
    # 再次回滚：版本切换语义（v1.1.0 已归档，回滚即切回）
    r = client.post("/api/v1/prompts/style-ab/rollback", headers=HEADERS)
    assert r.json()["version"] == "1.1.0"


def test_ab_experiment_split(client):
    _make_prompt(client, "style-ab2")
    # v2 保持草稿态：A 桶走当前发布指针（1.0.0），B 桶走实验指定的草稿版本
    client.post("/api/v1/prompts/style-ab2/versions", headers=HEADERS,
                json={"version": "2.0.0", "template": V2})
    # 实验指向不存在的版本 → 404
    r = client.post("/api/v1/prompts/experiments", headers=HEADERS,
                    json={"name": "exp-bad", "prompt": "style-ab2",
                          "version_a": "9.9.9", "version_b": "2.0.0"})
    assert r.status_code == 404
    # 实验 1：percent_b=100 → 全 B（2.0.0）
    r = client.post("/api/v1/prompts/experiments", headers=HEADERS,
                    json={"name": "exp-full-b", "prompt": "style-ab2",
                          "version_a": "1.0.0", "version_b": "2.0.0", "percent_b": 100})
    assert r.status_code == 200
    body = client.post("/api/v1/prompts/style-ab2/render", headers=HEADERS,
                       json={"variables": {"question": "q"}, "key": "user-x"}).json()
    assert body["version"] == "2.0.0" and body["experiment"]["picked"] == "b"

    # 实验 2（后建者优先生效）：percent_b=0 → 全 A（1.0.0，当前发布指针）
    client.post("/api/v1/prompts/experiments", headers=HEADERS,
                json={"name": "exp-half", "prompt": "style-ab2",
                      "version_a": "1.0.0", "version_b": "2.0.0", "percent_b": 0})
    body = client.post("/api/v1/prompts/style-ab2/render", headers=HEADERS,
                       json={"variables": {"question": "q"}, "key": "user-x"}).json()
    assert body["version"] == "1.0.0"

    # 停用实验 2 → 实验 1（percent 100）重新生效
    client.patch("/api/v1/prompts/experiments/exp-half?enabled=false", headers=HEADERS)
    body = client.post("/api/v1/prompts/style-ab2/render", headers=HEADERS,
                       json={"variables": {"question": "q"}, "key": "user-x"}).json()
    assert body["version"] == "2.0.0"

    # 停用实验 1 → 无实验 → 当前发布指针，experiment=None
    client.patch("/api/v1/prompts/experiments/exp-full-b?enabled=false", headers=HEADERS)
    body = client.post("/api/v1/prompts/style-ab2/render", headers=HEADERS,
                       json={"variables": {"question": "q"}, "key": "user-x"}).json()
    assert body["version"] == "1.0.0" and body["experiment"] is None


def test_ab_experiment_stable_split(client):
    """percent_b=50：不同 key 落入不同桶，同一 key 结果稳定。"""
    _make_prompt(client, "style-ab3")
    client.post("/api/v1/prompts/style-ab3/versions", headers=HEADERS,
                json={"version": "2.0.0", "template": V2})
    client.post("/api/v1/prompts/experiments", headers=HEADERS,
                json={"name": "exp-stable", "prompt": "style-ab3",
                      "version_a": "1.0.0", "version_b": "2.0.0", "percent_b": 50})
    seen = set()
    for i in range(20):
        body = client.post("/api/v1/prompts/style-ab3/render", headers=HEADERS,
                           json={"variables": {"question": "q"}, "key": f"u-{i}"}).json()
        seen.add(body["version"])
    assert seen == {"1.0.0", "2.0.0"}, seen
    # 稳定性：同一 key 多次渲染结果一致
    first = client.post("/api/v1/prompts/style-ab3/render", headers=HEADERS,
                        json={"variables": {"question": "q"}, "key": "fixed"}).json()["version"]
    for _ in range(3):
        again = client.post("/api/v1/prompts/style-ab3/render", headers=HEADERS,
                            json={"variables": {"question": "q"}, "key": "fixed"}).json()["version"]
        assert again == first


def test_faq_agent_uses_prompt_center(client):
    """faq-agent 接入 Prompt 中心：模板存在时回答链路正常（A/B 渲染进 system）。"""
    r = client.post("/api/v1/agents/faq-agent/invocations", headers=HEADERS,
                    json={"input": "如何创建知识库？", "session_id": "s-ab"})
    assert r.status_code == 200 and "[mock-llm]" in r.json()["output"]
    assert r.json()["citations"]

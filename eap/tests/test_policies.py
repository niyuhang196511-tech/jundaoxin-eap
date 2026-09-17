"""Policy Engine：模型白名单 / 数据不出域 / prompt 上限，网关强制执行（docs/02 ①）。"""

from __future__ import annotations


from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
CHAT = "/v1/chat/completions"
INVOKE = "/api/v1/agents/faq-agent/invocations"


def test_model_allowlist_blocks_and_recovers(client):
    # 平台默认（tenant_id=0）：只允许不存在的模型 → 租户 1 全部调用被拒
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "allow-only-nothing", "kind": "model-allowlist",
        "config": {"models": ["no-such-model"]}})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]
    r = client.post(INVOKE, headers=HEADERS, json={"input": "如何创建知识库？"})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]

    # 停用后恢复
    assert client.post("/api/v1/policies/allow-only-nothing/enabled?enabled=false",
                       headers=HEADERS).json()["enabled"] is False
    assert client.post(CHAT, headers=HEADERS,
                       json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 200


def test_provider_allowlist_data_boundary(client):
    """数据不出域：只允许 mock（本地）供应商时调用仍可用；外部模型被过滤。"""
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "local-only", "kind": "provider-allowlist",
        "config": {"providers": ["mock"]}, "priority": 50})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    assert r.json()["model"] in ("mock-llm", "external-llm")  # mock 供应商仍可用
    assert r.json()["model"] == "mock-llm"  # 外部 openai_compat 供应商被策略过滤
    client.post("/api/v1/policies/local-only/enabled?enabled=false", headers=HEADERS)


def test_tenant_policy_overrides_platform_default(client):
    """租户策略存在时平台默认不叠加：默认禁用一切，租户白名单 mock-llm 放行。"""
    client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "default-deny-all", "tenant_id": 0, "kind": "model-allowlist",
        "config": {"models": []}})
    client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "tenant1-allow-mock", "tenant_id": 1, "kind": "model-allowlist",
        "config": {"models": ["mock-llm"]}, "priority": 10})
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["model"] == "mock-llm"
    client.post("/api/v1/policies/default-deny-all/enabled?enabled=false", headers=HEADERS)
    client.post("/api/v1/policies/tenant1-allow-mock/enabled?enabled=false", headers=HEADERS)


def test_max_prompt_tokens_cap(client):
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "tiny-prompt-cap", "tenant_id": 1, "kind": "max-prompt-tokens",
        "config": {"limit": 5}})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS,
                    json={"messages": [{"role": "user", "content": "这句话足够长，用来触发单次调用 prompt token 上限策略的拒绝逻辑，估算值必定远超五个 token。"}]})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]
    client.post("/api/v1/policies/tiny-prompt-cap/enabled?enabled=false", headers=HEADERS)
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_policy_validation(client):
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "bad-kind", "kind": "model-allowlist", "config": {}})
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "bad-kind2", "kind": "magic"})
    assert r.status_code == 422  # pydantic pattern 拒绝未知类型

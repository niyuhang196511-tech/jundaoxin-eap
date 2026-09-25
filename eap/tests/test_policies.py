"""Policy Engine：模型白名单 / 数据不出域 / prompt 上限，网关强制执行（docs/02 ①）。

M52-D 可重入：策略名一律 uname 唯一化（脏库重跑不撞唯一约束）；并加模块级差量清理
（同 test_tool_governance）——策略按 kind 检索，残留启用策略会改变「租户自有策略
不叠加平台默认」语义，污染后续文件的网关断言。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
CHAT = "/v1/chat/completions"
INVOKE = "/api/v1/agents/faq-agent/invocations"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_policies(client: TestClient):
    """模块结束后删除本模块新增的策略行（差量清理，样板=test_tool_governance）。"""
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


def test_model_allowlist_blocks_and_recovers(client, uname):
    name = uname("allow-only-nothing")
    # 平台默认（tenant_id=0）：只允许不存在的模型 → 租户 1 全部调用被拒
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": name, "kind": "model-allowlist",
        "config": {"models": ["no-such-model"]}})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]
    r = client.post(INVOKE, headers=HEADERS, json={"input": "如何创建知识库？"})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]

    # 停用后恢复
    assert client.post(f"/api/v1/policies/{name}/enabled?enabled=false",
                       headers=HEADERS).json()["enabled"] is False
    assert client.post(CHAT, headers=HEADERS,
                       json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 200


def test_provider_allowlist_data_boundary(client, uname):
    """数据不出域：只允许 mock（本地）供应商时调用仍可用；外部模型被过滤。"""
    name = uname("local-only")
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": name, "kind": "provider-allowlist",
        "config": {"providers": ["mock"]}, "priority": 50})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    assert r.json()["model"] in ("mock-llm", "external-llm")  # mock 供应商仍可用
    assert r.json()["model"] == "mock-llm"  # 外部 openai_compat 供应商被策略过滤
    client.post(f"/api/v1/policies/{name}/enabled?enabled=false", headers=HEADERS)


def test_tenant_policy_overrides_platform_default(client, uname):
    """租户策略存在时平台默认不叠加：默认禁用一切，租户白名单 mock-llm 放行。"""
    deny = uname("default-deny-all")
    allow = uname("tenant1-allow-mock")
    client.post("/api/v1/policies", headers=HEADERS, json={
        "name": deny, "tenant_id": 0, "kind": "model-allowlist",
        "config": {"models": []}})
    client.post("/api/v1/policies", headers=HEADERS, json={
        "name": allow, "tenant_id": 1, "kind": "model-allowlist",
        "config": {"models": ["mock-llm"]}, "priority": 10})
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["model"] == "mock-llm"
    client.post(f"/api/v1/policies/{deny}/enabled?enabled=false", headers=HEADERS)
    client.post(f"/api/v1/policies/{allow}/enabled?enabled=false", headers=HEADERS)


def test_max_prompt_tokens_cap(client, uname):
    name = uname("tiny-prompt-cap")
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": name, "tenant_id": 1, "kind": "max-prompt-tokens",
        "config": {"limit": 5}})
    assert r.status_code == 200
    r = client.post(CHAT, headers=HEADERS,
                    json={"messages": [{"role": "user", "content": "这句话足够长，用来触发单次调用 prompt token 上限策略的拒绝逻辑，估算值必定远超五个 token。"}]})
    assert r.status_code == 403 and "EAP-7101" in r.json()["detail"]
    client.post(f"/api/v1/policies/{name}/enabled?enabled=false", headers=HEADERS)
    r = client.post(CHAT, headers=HEADERS, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_policy_validation(client):
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "bad-kind", "kind": "model-allowlist", "config": {}})
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = client.post("/api/v1/policies", headers=HEADERS, json={
        "name": "bad-kind2", "kind": "magic"})
    assert r.status_code == 422  # pydantic pattern 拒绝未知类型

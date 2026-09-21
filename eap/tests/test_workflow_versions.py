"""Workflow 版本化 + prod-env- 环境体系测试（M32）：
版本管线（草稿递增/发布归档/回滚）/ 执行解析链 / 存量兼容 / RBAC / workflow-as-agent 热更新。
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import WorkflowRecord, WorkflowVersionRecord
from eap.runtime.tools import Tool
from eap.runtime.workflow import register_workflow_tool

from .conftest import AUTH
from .test_oidc import ISSUER, _make_id_token, fake_idp  # noqa: F401 复用模拟 IdP 夹具

# ---------- 版本标记工具：handler 回显 value → 用 test-run/调用输出区分执行的是哪一版 DSL ----------

async def _marker_handler(args: str) -> str:
    payload = json.loads(args) if args else {}
    return f"wfver-marker:{payload.get('value', '')}"


register_workflow_tool("wfver.marker", lambda: Tool(
    name="wfver.marker", description="版本标记工具（测试）",
    parameters={"type": "object", "properties": {}}, handler=_marker_handler))


def _dsl(name: str, marker: str) -> dict:
    return {
        "name": name, "version": "1.0.0", "description": f"版本化工作流（{marker}）",
        "steps": [{"id": "mark", "type": "tool", "tool_name": "wfver.marker",
                   "tool_args": {"value": marker}}],
    }


def _make(client: TestClient, name: str, marker: str) -> None:
    resp = client.post("/api/v1/workflows", headers=AUTH, json=_dsl(name, marker))
    assert resp.status_code == 200, resp.text


def _draft(client: TestClient, name: str, note: str = "") -> dict:
    resp = client.post(f"/api/v1/workflows/{name}/versions", headers=AUTH, json={"note": note})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _publish(client: TestClient, name: str, version_id: int, env: str) -> dict:
    resp = client.post(f"/api/v1/workflows/{name}/versions/{version_id}/publish",
                       headers=AUTH, json={"env": env})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_record_dsl(name: str, marker: str) -> None:
    """模拟画布编辑后保存：直接更新 WorkflowRecord.dsl（无 update 端点，存草稿从此取）。"""
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        assert record is not None
        record.dsl = _dsl(name, marker)
        db.commit()


def _run_and_wait(client: TestClient, name: str, **body) -> dict:
    run_id = client.post(f"/api/v1/workflows/{name}/test-run", headers=AUTH,
                         json={"input": "hi", **body}).json()["run_id"]
    view: dict = {}
    for _ in range(30):
        view = client.get(f"/api/v1/workflows/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            return view
        time.sleep(0.2)
    return view


def _versions(client: TestClient, name: str) -> dict:
    return client.get(f"/api/v1/workflows/{name}/versions", headers=AUTH).json()


# ---------- 版本管线 ----------

def test_draft_versions_increment(client: TestClient):
    """存草稿：version 从 1 起单调递增；列表含 env/state/时间；初始无生产指针。"""
    _make(client, "wfver-inc-flow", "A")
    v1 = _draft(client, "wfver-inc-flow", note="首版")
    v2 = _draft(client, "wfver-inc-flow")
    assert (v1["version"], v1["state"]) == (1, "draft")
    assert v2["version"] == 2
    data = _versions(client, "wfver-inc-flow")
    assert data["published_version_id"] is None
    assert [v["version"] for v in data["versions"]] == [2, 1]  # 新→旧
    assert all(v["state"] == "draft" and v["env"] is None for v in data["versions"])
    assert data["versions"][0]["note"] == "" and data["versions"][1]["note"] == "首版"


def test_publish_env_pipeline(client: TestClient):
    """publish dev → 旧版 archived；同 env 二次发布拒绝；prod 发布同步生产指针。"""
    _make(client, "wfver-pipe-flow", "A")
    v1 = _draft(client, "wfver-pipe-flow")
    v2 = _draft(client, "wfver-pipe-flow")
    _publish(client, "wfver-pipe-flow", v1["id"], "dev")
    # v2 发布 dev → v1 归档
    _publish(client, "wfver-pipe-flow", v2["id"], "dev")
    rows = {v["version"]: v for v in _versions(client, "wfver-pipe-flow")["versions"]}
    assert rows[1]["state"] == "archived" and rows[1]["env"] == "dev"
    assert rows[2]["state"] == "published" and rows[2]["env"] == "dev"
    assert rows[2]["published_at"] is not None
    # 重复发布同一 env → 400
    resp = client.post(f"/api/v1/workflows/wfver-pipe-flow/versions/{v2['id']}/publish",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 400
    # 非法 env → 422（pydantic pattern）
    resp = client.post(f"/api/v1/workflows/wfver-pipe-flow/versions/{v2['id']}/publish",
                       headers=AUTH, json={"env": "qa"})
    assert resp.status_code == 422
    # prod 发布 → 指针同步
    _publish(client, "wfver-pipe-flow", v2["id"], "prod")
    data = _versions(client, "wfver-pipe-flow")
    assert data["published_version_id"] == v2["id"]


def test_publish_rejects_unparseable_dsl(client: TestClient):
    """发布期防线：坏 DSL 挡在发布前（复用 WorkflowSpec 解析校验），状态不变。"""
    _make(client, "wfver-bad-dsl-flow", "A")
    v1 = _draft(client, "wfver-bad-dsl-flow")
    with SessionLocal() as db:
        row = db.get(WorkflowVersionRecord, v1["id"])
        row.dsl = {"name": "wfver-bad-dsl-flow", "steps": "not-a-list"}
        db.commit()
    resp = client.post(f"/api/v1/workflows/wfver-bad-dsl-flow/versions/{v1['id']}/publish",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 400
    with SessionLocal() as db:
        assert db.get(WorkflowVersionRecord, v1["id"]).state == "draft"


def test_rollback_restores_previous(client: TestClient):
    """rollback：该 env 上一 archived 重发布、现 published 归档；prod 回滚同步指针。"""
    _make(client, "wfver-rollback-flow", "A")
    v1 = _draft(client, "wfver-rollback-flow")
    v2 = _draft(client, "wfver-rollback-flow")
    # prod 上 v1 → v2，指针指向 v2
    _publish(client, "wfver-rollback-flow", v1["id"], "prod")
    _publish(client, "wfver-rollback-flow", v2["id"], "prod")
    assert _versions(client, "wfver-rollback-flow")["published_version_id"] == v2["id"]
    # 回滚 prod → v1 恢复发布，v2 归档，指针同步
    resp = client.post(f"/api/v1/workflows/wfver-rollback-flow/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1
    rows = {v["version"]: v for v in _versions(client, "wfver-rollback-flow")["versions"]}
    assert rows[1]["state"] == "published" and rows[1]["env"] == "prod"
    assert rows[2]["state"] == "archived"
    assert _versions(client, "wfver-rollback-flow")["published_version_id"] == v1["id"]
    # 再次回滚（当前 published=v1）→ 翻转回 v2（与 agents 回滚语义一致：最近归档重发布）
    resp = client.post(f"/api/v1/workflows/wfver-rollback-flow/versions/{v1['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200 and resp.json()["version"] == 2
    assert _versions(client, "wfver-rollback-flow")["published_version_id"] == v2["id"]
    # 其他工作流的 dev 环境无版本 → 409
    _make(client, "wfver-rollback-empty", "A")
    v = _draft(client, "wfver-rollback-empty")
    _publish(client, "wfver-rollback-empty", v["id"], "prod")
    resp = client.post(f"/api/v1/workflows/wfver-rollback-empty/versions/{v['id']}/rollback",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 409


def test_diff_endpoint(client: TestClient):
    """diff：两版 DSL 结构化差异（节点字段级），形态对齐 agent diff 端点。"""
    _make(client, "wfver-diff-flow", "A")
    v1 = _draft(client, "wfver-diff-flow")
    _set_record_dsl("wfver-diff-flow", "B")
    v2 = _draft(client, "wfver-diff-flow")
    resp = client.get(f"/api/v1/workflows/wfver-diff-flow/versions/{v1['id']}/diff/{v2['id']}",
                      headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["from"], data["to"]) == (v1["id"], v2["id"])
    keys = {c["key"] for c in data["changes"]}
    assert "steps.mark.tool_args" in keys  # marker A → B
    arg_change = next(c for c in data["changes"] if c["key"] == "steps.mark.tool_args")
    assert arg_change["from"] == {"value": "A"} and arg_change["to"] == {"value": "B"}
    # 不存在的版本 → 404
    resp = client.get(f"/api/v1/workflows/wfver-diff-flow/versions/{v1['id']}/diff/999999",
                      headers=AUTH)
    assert resp.status_code == 404


# ---------- 执行解析链：显式 version > env 指定 > prod 指针 > dsl 兜底 ----------

def test_resolution_chain(client: TestClient):
    """两内容不同的版本断言执行走对版本：test-run 输出携带 wfver-marker:<value>。"""
    _make(client, "wfver-chain-flow", "A")
    v1 = _draft(client, "wfver-chain-flow")          # v1 dsl = marker A
    _set_record_dsl("wfver-chain-flow", "B")
    v2 = _draft(client, "wfver-chain-flow")          # v2 dsl = marker B（画布编辑后存的新草稿）
    assert v1["version"] == 1 and v2["version"] == 2

    # prod 发布 v1：指针 → v1；缺省与 env=prod 都走 A
    _publish(client, "wfver-chain-flow", v1["id"], "prod")
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow", env="prod")["output"]

    # v2 晋升 dev：env=dev 走 B（env 指定优先于 prod 指针 v1→A）；缺省仍走指针 A
    _publish(client, "wfver-chain-flow", v2["id"], "dev")
    assert "wfver-marker:B" in _run_and_wait(client, "wfver-chain-flow", env="dev")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow", env="prod")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow")["output"]

    # v2 晋升 prod：指针 → v2；缺省走 B；显式 version 精确命中（优先于指针/env）
    _publish(client, "wfver-chain-flow", v2["id"], "prod")
    assert "wfver-marker:B" in _run_and_wait(client, "wfver-chain-flow")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow", version=1)["output"]
    assert "wfver-marker:B" in _run_and_wait(client, "wfver-chain-flow", version=2)["output"]

    # prod 回滚 → v1：缺省走 A（指针生效，而非 dsl 字段的 B）
    resp = client.post(f"/api/v1/workflows/wfver-chain-flow/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-chain-flow")["output"]


def test_no_versions_falls_back_to_dsl(client: TestClient):
    """存量无版本工作流行为不变：解析链兜底 WorkflowRecord.dsl，env 覆盖也落兜底。"""
    _make(client, "wfver-legacy-flow", "A")
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-legacy-flow")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, "wfver-legacy-flow", env="dev")["output"]
    # 显式 version 不存在 → 404
    resp = client.post("/api/v1/workflows/wfver-legacy-flow/test-run", headers=AUTH,
                       json={"input": "hi", "version": 9})
    assert resp.status_code == 404


# ---------- workflow-as-agent 热更新（publish/rollback 后立即用新版） ----------

def test_workflow_agent_hot_update(client: TestClient):
    """publish/rollback 后注册的 spec 变化：按 prod 指针调用智能体，输出随版本切换。"""
    _make(client, "wfver-agent-flow", "A")
    v1 = _draft(client, "wfver-agent-flow")
    _set_record_dsl("wfver-agent-flow", "B")
    v2 = _draft(client, "wfver-agent-flow")

    def _invoke() -> str:
        resp = client.post("/api/v1/agents/wfver-agent-flow/invocations", headers=AUTH,
                           json={"input": "hi"})
        assert resp.status_code == 200, resp.text
        return resp.json()["output"]

    # 发布前：注册的智能体类在创建时烘焙 dsl（A），无指针、无重注册不随 dsl 字段漂移
    assert "wfver-marker:A" in _invoke()
    # 发布 v1 → prod：热更新（v1 dsl 同为 A）
    _publish(client, "wfver-agent-flow", v1["id"], "prod")
    assert "wfver-marker:A" in _invoke()
    # 发布 v2 → prod：热更新为 B
    _publish(client, "wfver-agent-flow", v2["id"], "prod")
    assert "wfver-marker:B" in _invoke()
    # 回滚 → v1：智能体切回 A
    resp = client.post(f"/api/v1/workflows/wfver-agent-flow/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200
    assert "wfver-marker:A" in _invoke()


# ---------- RBAC ----------

def test_member_cannot_write_versions(client: TestClient, fake_idp):  # noqa: F811
    """member 角色可读版本列表，写操作（存草稿/发布/回滚）403。"""
    _make(client, "wfver-rbac-flow", "A")
    member = _make_id_token({"iss": ISSUER, "sub": "member-wfver@corp", "exp": int(time.time()) + 600,
                             "tenant_id": 1, "roles": ["member"]})
    m = {"Authorization": f"Bearer {member}"}
    assert client.get("/api/v1/workflows/wfver-rbac-flow/versions", headers=m).status_code == 200
    assert client.post("/api/v1/workflows/wfver-rbac-flow/versions", headers=m,
                       json={"note": ""}).status_code == 403
    assert client.post("/api/v1/workflows/wfver-rbac-flow/versions/1/publish", headers=m,
                       json={"env": "dev"}).status_code == 403
    assert client.post("/api/v1/workflows/wfver-rbac-flow/versions/1/rollback", headers=m,
                       json={"env": "dev"}).status_code == 403

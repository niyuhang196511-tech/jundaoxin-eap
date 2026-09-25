"""Workflow 版本化 + prod-env- 环境体系测试（M32）：
版本管线（草稿递增/发布归档/回滚）/ 执行解析链 / 存量兼容 / RBAC / workflow-as-agent 热更新。

M52-D 可重入：工作流名一律加模块级 uuid 后缀（脏库重跑不撞「工作流已存在」，
且版本列表/指针断言天然按本运行自造的 flow 隔离）；模块结束把自造 flow 停用，
避免残留 enabled 工作流在后续运行启动时重复注册进智能体注册表。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import WorkflowRecord, WorkflowVersionRecord
from eap.runtime.tools import Tool
from eap.runtime.workflow import register_workflow_tool

from .conftest import AUTH
from .test_oidc import ISSUER, _make_id_token, fake_idp  # noqa: F401 复用模拟 IdP 夹具

# ---------- M52-D 可重入命名（样板=test_connector_v2._SFX） ----------
_SFX = uuid.uuid4().hex[:8]

F_INC = f"wfver-inc-flow-{_SFX}"
F_PIPE = f"wfver-pipe-flow-{_SFX}"
F_BAD_DSL = f"wfver-bad-dsl-flow-{_SFX}"
F_ROLL = f"wfver-rollback-flow-{_SFX}"
F_ROLL_EMPTY = f"wfver-rollback-empty-{_SFX}"
F_DIFF = f"wfver-diff-flow-{_SFX}"
F_DIFF_DRAFT = f"wfver-diffdraft-flow-{_SFX}"
F_DIFF_PAIR = f"wfver-diffpair-flow-{_SFX}"
F_CHAIN = f"wfver-chain-flow-{_SFX}"
F_LEGACY = f"wfver-legacy-flow-{_SFX}"
F_AGENT = f"wfver-agent-flow-{_SFX}"
F_RBAC = f"wfver-rbac-flow-{_SFX}"

_ALL_FLOWS = (F_INC, F_PIPE, F_BAD_DSL, F_ROLL, F_ROLL_EMPTY,
              F_DIFF, F_DIFF_DRAFT, F_DIFF_PAIR, F_CHAIN, F_LEGACY, F_AGENT, F_RBAC)


@pytest.fixture(scope="module", autouse=True)
def _disable_created_flows(client: TestClient):
    """模块结束后停用本模块创建的工作流（软删；不留在库中作为 enabled 智能体残留）。"""
    yield
    for name in _ALL_FLOWS:
        client.delete(f"/api/v1/workflows/{name}", headers=AUTH)  # 404/失败无害


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
    _put_dsl(name, _dsl(name, marker))


def _put_dsl(name: str, dsl: dict) -> None:
    """覆写 WorkflowRecord.dsl 为任意 DSL（diff 测试需要带 edges/多步骤的定制 DSL）。"""
    with SessionLocal() as db:
        record = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == name))
        assert record is not None
        record.dsl = dsl
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
    _make(client, F_INC, "A")
    v1 = _draft(client, F_INC, note="首版")
    v2 = _draft(client, F_INC)
    assert (v1["version"], v1["state"]) == (1, "draft")
    assert v2["version"] == 2
    data = _versions(client, F_INC)
    assert data["published_version_id"] is None
    assert [v["version"] for v in data["versions"]] == [2, 1]  # 新→旧
    assert all(v["state"] == "draft" and v["env"] is None for v in data["versions"])
    assert data["versions"][0]["note"] == "" and data["versions"][1]["note"] == "首版"


def test_publish_env_pipeline(client: TestClient):
    """publish dev → 旧版 archived；同 env 二次发布拒绝；prod 发布同步生产指针。"""
    _make(client, F_PIPE, "A")
    v1 = _draft(client, F_PIPE)
    v2 = _draft(client, F_PIPE)
    _publish(client, F_PIPE, v1["id"], "dev")
    # v2 发布 dev → v1 归档
    _publish(client, F_PIPE, v2["id"], "dev")
    rows = {v["version"]: v for v in _versions(client, F_PIPE)["versions"]}
    assert rows[1]["state"] == "archived" and rows[1]["env"] == "dev"
    assert rows[2]["state"] == "published" and rows[2]["env"] == "dev"
    assert rows[2]["published_at"] is not None
    # 重复发布同一 env → 400
    resp = client.post(f"/api/v1/workflows/{F_PIPE}/versions/{v2['id']}/publish",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 400
    # 非法 env → 422（pydantic pattern）
    resp = client.post(f"/api/v1/workflows/{F_PIPE}/versions/{v2['id']}/publish",
                       headers=AUTH, json={"env": "qa"})
    assert resp.status_code == 422
    # prod 发布 → 指针同步
    _publish(client, F_PIPE, v2["id"], "prod")
    data = _versions(client, F_PIPE)
    assert data["published_version_id"] == v2["id"]


def test_publish_rejects_unparseable_dsl(client: TestClient):
    """发布期防线：坏 DSL 挡在发布前（复用 WorkflowSpec 解析校验），状态不变。"""
    _make(client, F_BAD_DSL, "A")
    v1 = _draft(client, F_BAD_DSL)
    with SessionLocal() as db:
        row = db.get(WorkflowVersionRecord, v1["id"])
        row.dsl = {"name": F_BAD_DSL, "steps": "not-a-list"}
        db.commit()
    resp = client.post(f"/api/v1/workflows/{F_BAD_DSL}/versions/{v1['id']}/publish",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 400
    with SessionLocal() as db:
        assert db.get(WorkflowVersionRecord, v1["id"]).state == "draft"


def test_rollback_restores_previous(client: TestClient):
    """rollback：该 env 上一 archived 重发布、现 published 归档；prod 回滚同步指针。"""
    _make(client, F_ROLL, "A")
    v1 = _draft(client, F_ROLL)
    v2 = _draft(client, F_ROLL)
    # prod 上 v1 → v2，指针指向 v2
    _publish(client, F_ROLL, v1["id"], "prod")
    _publish(client, F_ROLL, v2["id"], "prod")
    assert _versions(client, F_ROLL)["published_version_id"] == v2["id"]
    # 回滚 prod → v1 恢复发布，v2 归档，指针同步
    resp = client.post(f"/api/v1/workflows/{F_ROLL}/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1
    rows = {v["version"]: v for v in _versions(client, F_ROLL)["versions"]}
    assert rows[1]["state"] == "published" and rows[1]["env"] == "prod"
    assert rows[2]["state"] == "archived"
    assert _versions(client, F_ROLL)["published_version_id"] == v1["id"]
    # 再次回滚（当前 published=v1）→ 翻转回 v2（与 agents 回滚语义一致：最近归档重发布）
    resp = client.post(f"/api/v1/workflows/{F_ROLL}/versions/{v1['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200 and resp.json()["version"] == 2
    assert _versions(client, F_ROLL)["published_version_id"] == v2["id"]
    # 其他工作流的 dev 环境无版本 → 409
    _make(client, F_ROLL_EMPTY, "A")
    v = _draft(client, F_ROLL_EMPTY)
    _publish(client, F_ROLL_EMPTY, v["id"], "prod")
    resp = client.post(f"/api/v1/workflows/{F_ROLL_EMPTY}/versions/{v['id']}/rollback",
                       headers=AUTH, json={"env": "dev"})
    assert resp.status_code == 409


def test_diff_endpoint(client: TestClient):
    """diff：两版 DSL 结构化差异（节点字段级），形态对齐 agent diff 端点。"""
    _make(client, F_DIFF, "A")
    v1 = _draft(client, F_DIFF)
    _set_record_dsl(F_DIFF, "B")
    v2 = _draft(client, F_DIFF)
    resp = client.get(f"/api/v1/workflows/{F_DIFF}/versions/{v1['id']}/diff/{v2['id']}",
                      headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["from"], data["to"]) == (v1["id"], v2["id"])
    keys = {c["key"] for c in data["changes"]}
    assert "steps.mark.tool_args" in keys  # marker A → B
    arg_change = next(c for c in data["changes"] if c["key"] == "steps.mark.tool_args")
    assert arg_change["from"] == {"value": "A"} and arg_change["to"] == {"value": "B"}
    # 不存在的版本 → 404（版本 id 全局自增，脏库下取远超实际水位的 id 保证不存在）
    resp = client.get(f"/api/v1/workflows/{F_DIFF}/versions/{v1['id']}/diff/99999999",
                      headers=AUTH)
    assert resp.status_code == 404


def test_diff_against_draft(client: TestClient):
    """M54-B diff?against=draft：版本对当前草稿——修改/新增/删除步骤 + edges 差异各现形。

    只读端点不落审计（平台惯例仅写操作记审计，与 GET /{name}/versions 一致），
    本用例不触发任何 audit.record 断言即其佐证：无需 mock 审计写入。
    """
    _make(client, F_DIFF_DRAFT, "A")
    _put_dsl(F_DIFF_DRAFT, {
        "name": F_DIFF_DRAFT, "version": "1.0.0", "description": "diff 基准",
        "steps": [
            {"id": "mark", "type": "tool", "tool_name": "wfver.marker",
             "tool_args": {"value": "A"}},
            {"id": "old", "type": "llm", "system": "将被删除"},
        ],
        "edges": [{"id": "e-old", "source": "mark", "target": "old", "source_handle": None}],
    })
    v1 = _draft(client, F_DIFF_DRAFT)
    _put_dsl(F_DIFF_DRAFT, {
        "name": F_DIFF_DRAFT, "version": "1.0.0", "description": "diff 对比",
        "steps": [
            {"id": "mark", "type": "tool", "tool_name": "wfver.marker",
             "tool_args": {"value": "B"}},  # 修改
            {"id": "new", "type": "tool", "tool_name": "wfver.marker",
             "tool_args": {"value": "C"}},  # 新增
        ],  # old 步骤被删除
        "edges": [{"id": "e-new", "source": "mark", "target": "new", "source_handle": None}],
    })
    resp = client.get(f"/api/v1/workflows/{F_DIFF_DRAFT}/versions/{v1['id']}/diff"
                      "?against=draft", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["workflow"], data["from"], data["to"]) == (F_DIFF_DRAFT, v1["id"], "draft")
    assert data["from_meta"]["version"] == 1 and data["to_meta"] is None  # 草稿侧无版本元信息
    changes = {c["key"]: c for c in data["changes"]}
    # 修改：tool_args 逐字段旧→新
    assert changes["steps.mark.tool_args"]["from"] == {"value": "A"}
    assert changes["steps.mark.tool_args"]["to"] == {"value": "B"}
    # 删除（to=None）/ 新增（from=None）
    assert changes["steps.old"]["to"] is None and changes["steps.old"]["from"]["id"] == "old"
    assert changes["steps.new"]["from"] is None and changes["steps.new"]["to"]["id"] == "new"
    # edges 差异
    assert changes["edges.e-old"]["to"] is None and changes["edges.e-new"]["from"] is None
    # 顶层元信息（description）也在 diff 内
    assert changes["description"]["from"] == "diff 基准" and changes["description"]["to"] == "diff 对比"


def test_diff_against_version_and_errors(client: TestClient):
    """M54-B diff?against={id}：版本对版本（带 to_meta 元信息）；同版本空差异；404/400 语义。"""
    _make(client, F_DIFF_PAIR, "A")
    _put_dsl(F_DIFF_PAIR, {
        "name": F_DIFF_PAIR, "version": "1.0.0", "description": "v1",
        "steps": [{"id": "mark", "type": "tool", "tool_name": "wfver.marker",
                   "tool_args": {"value": "A"}}],
    })
    v1 = _draft(client, F_DIFF_PAIR)
    _put_dsl(F_DIFF_PAIR, {
        "name": F_DIFF_PAIR, "version": "1.0.0", "description": "v2",
        "steps": [{"id": "mark", "type": "tool", "tool_name": "wfver.marker",
                   "tool_args": {"value": "B"}}],
    })
    v2 = _draft(client, F_DIFF_PAIR)
    # 版本对版本：to 侧带版本元信息
    resp = client.get(f"/api/v1/workflows/{F_DIFF_PAIR}/versions/{v1['id']}/diff"
                      f"?against={v2['id']}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["to"] == v2["id"]
    assert data["to_meta"]["version"] == 2 and data["to_meta"]["state"] == "draft"
    assert "steps.mark.tool_args" in {c["key"] for c in data["changes"]}
    # 同版本自比：无差异（空列表 → 前端空态）
    resp = client.get(f"/api/v1/workflows/{F_DIFF_PAIR}/versions/{v1['id']}/diff"
                      f"?against={v1['id']}", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["changes"] == []
    # 不存在的 against 版本 → 404；不存在的基础版本 → 404；非法 against → 400
    resp = client.get(f"/api/v1/workflows/{F_DIFF_PAIR}/versions/{v1['id']}/diff"
                      "?against=99999999", headers=AUTH)
    assert resp.status_code == 404
    resp = client.get(f"/api/v1/workflows/{F_DIFF_PAIR}/versions/99999999/diff"
                      "?against=draft", headers=AUTH)
    assert resp.status_code == 404
    resp = client.get(f"/api/v1/workflows/{F_DIFF_PAIR}/versions/{v1['id']}/diff"
                      "?against=nonsense", headers=AUTH)
    assert resp.status_code == 400


# ---------- 执行解析链：显式 version > env 指定 > prod 指针 > dsl 兜底 ----------

def test_resolution_chain(client: TestClient):
    """两内容不同的版本断言执行走对版本：test-run 输出携带 wfver-marker:<value>。"""
    _make(client, F_CHAIN, "A")
    v1 = _draft(client, F_CHAIN)          # v1 dsl = marker A
    _set_record_dsl(F_CHAIN, "B")
    v2 = _draft(client, F_CHAIN)          # v2 dsl = marker B（画布编辑后存的新草稿）
    assert v1["version"] == 1 and v2["version"] == 2

    # prod 发布 v1：指针 → v1；缺省与 env=prod 都走 A
    _publish(client, F_CHAIN, v1["id"], "prod")
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN)["output"]
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN, env="prod")["output"]

    # v2 晋升 dev：env=dev 走 B（env 指定优先于 prod 指针 v1→A）；缺省仍走指针 A
    _publish(client, F_CHAIN, v2["id"], "dev")
    assert "wfver-marker:B" in _run_and_wait(client, F_CHAIN, env="dev")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN, env="prod")["output"]
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN)["output"]

    # v2 晋升 prod：指针 → v2；缺省走 B；显式 version 精确命中（优先于指针/env）
    _publish(client, F_CHAIN, v2["id"], "prod")
    assert "wfver-marker:B" in _run_and_wait(client, F_CHAIN)["output"]
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN, version=1)["output"]
    assert "wfver-marker:B" in _run_and_wait(client, F_CHAIN, version=2)["output"]

    # prod 回滚 → v1：缺省走 A（指针生效，而非 dsl 字段的 B）
    resp = client.post(f"/api/v1/workflows/{F_CHAIN}/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200
    assert "wfver-marker:A" in _run_and_wait(client, F_CHAIN)["output"]


def test_no_versions_falls_back_to_dsl(client: TestClient):
    """存量无版本工作流行为不变：解析链兜底 WorkflowRecord.dsl，env 覆盖也落兜底。"""
    _make(client, F_LEGACY, "A")
    assert "wfver-marker:A" in _run_and_wait(client, F_LEGACY)["output"]
    assert "wfver-marker:A" in _run_and_wait(client, F_LEGACY, env="dev")["output"]
    # 显式 version 不存在 → 404
    resp = client.post(f"/api/v1/workflows/{F_LEGACY}/test-run", headers=AUTH,
                       json={"input": "hi", "version": 99999999})
    assert resp.status_code == 404


# ---------- workflow-as-agent 热更新（publish/rollback 后立即用新版） ----------

def test_workflow_agent_hot_update(client: TestClient):
    """publish/rollback 后注册的 spec 变化：按 prod 指针调用智能体，输出随版本切换。"""
    _make(client, F_AGENT, "A")
    v1 = _draft(client, F_AGENT)
    _set_record_dsl(F_AGENT, "B")
    v2 = _draft(client, F_AGENT)

    def _invoke() -> str:
        resp = client.post(f"/api/v1/agents/{F_AGENT}/invocations", headers=AUTH,
                           json={"input": "hi"})
        assert resp.status_code == 200, resp.text
        return resp.json()["output"]

    # 发布前：注册的智能体类在创建时烘焙 dsl（A），无指针、无重注册不随 dsl 字段漂移
    assert "wfver-marker:A" in _invoke()
    # 发布 v1 → prod：热更新（v1 dsl 同为 A）
    _publish(client, F_AGENT, v1["id"], "prod")
    assert "wfver-marker:A" in _invoke()
    # 发布 v2 → prod：热更新为 B
    _publish(client, F_AGENT, v2["id"], "prod")
    assert "wfver-marker:B" in _invoke()
    # 回滚 → v1：智能体切回 A
    resp = client.post(f"/api/v1/workflows/{F_AGENT}/versions/{v2['id']}/rollback",
                       headers=AUTH, json={"env": "prod"})
    assert resp.status_code == 200
    assert "wfver-marker:A" in _invoke()


# ---------- RBAC ----------

def test_member_cannot_write_versions(client: TestClient, fake_idp):  # noqa: F811
    """member 角色可读版本列表与 diff，写操作（存草稿/发布/回滚）403。"""
    _make(client, F_RBAC, "A")
    v = _draft(client, F_RBAC)
    member = _make_id_token({"iss": ISSUER, "sub": "member-wfver@corp", "exp": int(time.time()) + 600,
                             "tenant_id": 1, "roles": ["member"]})
    m = {"Authorization": f"Bearer {member}"}
    assert client.get(f"/api/v1/workflows/{F_RBAC}/versions", headers=m).status_code == 200
    # diff 只读端点鉴权对齐版本列表（无需 admin，M54-B）
    assert client.get(f"/api/v1/workflows/{F_RBAC}/versions/{v['id']}/diff?against=draft",
                      headers=m).status_code == 200
    assert client.post(f"/api/v1/workflows/{F_RBAC}/versions", headers=m,
                       json={"note": ""}).status_code == 403
    assert client.post(f"/api/v1/workflows/{F_RBAC}/versions/1/publish", headers=m,
                       json={"env": "dev"}).status_code == 403
    assert client.post(f"/api/v1/workflows/{F_RBAC}/versions/1/rollback", headers=m,
                       json={"env": "dev"}).status_code == 403

"""Action + Artifact 测试（v0.5-⑥⑦）：动作直调/审批路径、产物存储/预览/下载。"""

from __future__ import annotations


from fastapi.testclient import TestClient

from .conftest import AUTH


def test_action_invoke_executes_connector_tool(client: TestClient):
    """直调：UI 动作 → 连接器工具（mock-erp 库存查询）→ 结果落会话记忆。"""
    resp = client.post("/api/v1/actions/invoke", headers=AUTH, json={
        "action": "查库存", "tool": "erp.inventory.query",
        "args": {"product": "EAP 一体机"},
        "session_id": "act-e2e-1", "agent": "warehouse-agent",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "executed"
    assert "EAP 一体机" in data["result"]
    # 结果写会话记忆（tool 角色）
    msgs = client.get("/api/v1/conversations/act-e2e-1/messages", headers=AUTH).json()
    assert any(m["role"] == "tool" and "查库存" in m["content"] for m in msgs)


def test_action_requires_approval_routes_to_hitl(client: TestClient):
    """高风险动作（order.create requires_approval）→ 转审批任务（WAITING_HUMAN）。"""
    resp = client.post("/api/v1/actions/invoke", headers=AUTH, json={
        "action": "创建订单", "tool": "erp.order.create",
        "args": {"product": "EAP 网关", "qty": 2},
        "session_id": "act-e2e-2", "agent": "warehouse-agent",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "approval_required"
    task_id = data["task_id"]
    view = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
    assert view["state"] == "WAITING_HUMAN"
    # 审批通过后可从任务结果看到动作参数
    assert view["result"]["pending"]["tool"] == "erp.order.create"


def test_action_unknown_tool_400(client: TestClient):
    resp = client.post("/api/v1/actions/invoke", headers=AUTH,
                       json={"action": "x", "tool": "no.such.tool", "args": {}})
    assert resp.status_code == 400


def test_artifact_create_preview_download(client: TestClient):
    """产物：创建（会话内服务端落盘）→ 列表 → 预览（内联文本）→ 下载。"""
    import uuid as _uuid

    session_id = "art-e2e-" + _uuid.uuid4().hex[:6]
    # 用一个演示 agent 走 SDK 创建产物
    resp = client.post("/api/v1/agents/warehouse-agent/invocations", headers=AUTH,
                       json={"input": "补货", "session_id": session_id})
    interaction = resp.json()["interaction"]
    assert interaction
    resp = client.post(f"/api/v1/agents/warehouse-agent/interactions/{interaction['id']}/submit",
                       headers=AUTH, json={"values": {"product": "EAP 一体机", "qty": 5},
                                            "session_id": session_id})
    assert resp.status_code == 200, resp.text

    # 直接经 SDK 创建产物（模拟 agent 产物生成）
    import asyncio

    from eap.runtime.artifacts import create_artifact

    async def _make():
        from eap.db import SessionLocal

        with SessionLocal() as db:
            record = create_artifact(
                db, name="补货建议", type="md",
                content="# 补货建议\n\nEAP 一体机 × 5",
                agent="warehouse-agent", session_id=session_id,
            )
            db.commit()
            return record.id

    art_id = asyncio.run(_make())
    listing = client.get("/api/v1/artifacts", headers=AUTH,
                         params={"session_id": session_id}).json()
    assert any(a["id"] == art_id for a in listing)

    preview = client.get(f"/api/v1/artifacts/{art_id}/preview", headers=AUTH)
    assert preview.status_code == 200
    assert preview.json()["content"].startswith("# 补货建议")

    download = client.get(f"/api/v1/artifacts/{art_id}/download", headers=AUTH)
    assert download.status_code == 200
    assert "补货建议" in download.text

    # 不存在的产物 → 404
    assert client.get("/api/v1/artifacts/art-nonexistent00/preview",
                      headers=AUTH).status_code == 404


def test_artifact_expired_not_listed(client: TestClient):
    """过期产物：get_artifact 惰性剔除。"""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from eap.db import SessionLocal
    from eap.runtime.artifacts import create_artifact, get_artifact

    async def _make():
        with SessionLocal() as db:
            record = create_artifact(db, name="过期产物", type="json", content="{}",
                                     ttl_hours=1)
            record.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
            db.commit()
            return record.id

    art_id = asyncio.run(_make())
    with SessionLocal() as db:
        assert get_artifact(db, art_id) is None

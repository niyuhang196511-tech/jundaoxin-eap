"""Harness 审计上报测试（M41-B，docs/18 M40 后置项）：

设备 Key 上报入库（action 前缀强制改写）/ 无凭证 401 / 非设备 Key 403 /
批量上限 422 / device_user 进 actor / 上报动作自审计 harness.audit.report。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def _register(client: TestClient, name: str, user: str | None = None) -> dict:
    body: dict = {"name": name}
    if user:
        body["user"] = user
    r = client.post("/api/v1/auth/devices", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _report(client: TestClient, key: str, entries: list[dict]):
    return client.post("/api/v1/audit/harness-report",
                       headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {key}"},
                       json={"entries": entries})


def _query_audit(client: TestClient, **params: str) -> list[dict]:
    r = client.get("/api/v1/audit", headers=AUTH, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_device_report_entries_and_prefix_force(client: TestClient):
    """设备 Key 上报入库：无前缀 action 改写补 harness.（防伪造他类审计），target=设备名。"""
    key = _register(client, "rep-dev-1", user="zhang.san")["api_key"]
    r = _report(client, key, [
        {"action": "skill.run", "detail": "demo-fixture/scripts/main.py", "created_at": "1758500000"},
        {"action": "skill.install", "detail": "demo-fixture@1.2.3"},
        {"action": "harness.already.prefixed", "detail": ""},
        {"action": "", "detail": "无 action 条目跳过"},
        "非 dict 条目跳过",
    ])
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3}
    rows = _query_audit(client, actor="harness:rep-dev-1:zhang.san")
    by_action = {row["action"]: row for row in rows}
    assert "harness.skill.run" in by_action
    assert "harness.skill.install" in by_action
    assert "harness.already.prefixed" in by_action  # 已有前缀原样保留（不双重叠加）
    run = by_action["harness.skill.run"]
    assert run["target"] == "rep-dev-1"
    assert run["detail"] == {"detail": "demo-fixture/scripts/main.py"}
    assert run["created_at"].startswith("2025-09")  # epoch 秒 1758500000 → 本地发生时刻
    # 伪造他类审计不可达：平台 action 空间无未加前缀条目
    assert all(row["action"].startswith("harness.") for row in rows)


def test_report_requires_auth_401(client: TestClient):
    r = client.post("/api/v1/audit/harness-report", json={"entries": []})
    assert r.status_code == 401


def test_report_requires_device_key_403(client: TestClient):
    """非设备凭证（管理 API Key/JWT 等任意有效凭证）无从归属设备 → 403。"""
    r = client.post("/api/v1/audit/harness-report", headers=HEADERS, json={"entries": []})
    assert r.status_code == 403


def test_report_batch_limit_422(client: TestClient):
    key = _register(client, "rep-dev-2")["api_key"]
    r = _report(client, key, [{"action": "skill.run", "detail": f"n{i}"} for i in range(501)])
    assert r.status_code == 422
    assert "500" in r.json()["detail"]


def test_device_user_placeholder_in_actor(client: TestClient):
    """M40-A 设备-用户配对：注册未指定 user → actor 占位符 ?。"""
    key = _register(client, "rep-dev-3")["api_key"]
    assert _report(client, key, [{"action": "skill.run", "detail": "x"}]).json() == {"accepted": 1}
    rows = _query_audit(client, actor="harness:rep-dev-3:?")
    assert len(rows) >= 1
    assert all(row["target"] == "rep-dev-3" for row in rows)


def test_report_self_audit(client: TestClient):
    """上报动作本身落 harness.audit.report（仅条数，不含条目内容）。"""
    key = _register(client, "rep-dev-4", user="li.si")["api_key"]
    assert _report(client, key, [
        {"action": "skill.run", "detail": "a"},
        {"action": "skill.remove", "detail": "b"},
    ]).json() == {"accepted": 2}
    rows = _query_audit(client, action="harness.audit.report", actor="harness:rep-dev-4:li.si")
    assert len(rows) == 1
    assert rows[0]["detail"] == {"count": 2}
    assert "skill.run" not in __import__("json").dumps(rows[0]["detail"])  # 不含条目内容


def test_report_created_at_iso_and_missing(client: TestClient):
    """created_at 兼容 ISO 文本；缺失用当前时刻（不落 1970）。"""
    key = _register(client, "rep-dev-5")["api_key"]
    r = _report(client, key, [
        {"action": "skill.run", "detail": "iso", "created_at": "2026-01-15T08:30:00+00:00"},
        {"action": "skill.run", "detail": "missing", "created_at": ""},
        {"action": "skill.run", "detail": "garbage", "created_at": "not-a-time"},
    ])
    assert r.json() == {"accepted": 3}
    rows = _query_audit(client, actor="harness:rep-dev-5:?")  # 含条目行与上报自审计行
    iso_row = next(row for row in rows if (row["detail"] or {}).get("detail") == "iso")
    assert iso_row["created_at"].startswith("2026-01-15")
    for marker in ("missing", "garbage"):
        row = next(row for row in rows if (row["detail"] or {}).get("detail") == marker)
        assert row["created_at"].startswith("2026-")  # 当前时刻，非 epoch 0

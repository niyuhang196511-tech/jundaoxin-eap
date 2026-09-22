"""Harness 设备绑定测试（M37）：签发（明文仅一次）/列表/吊销/重名 409/RBAC。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def _register(client: TestClient, name: str):
    return client.post("/api/v1/auth/devices", headers=HEADERS, json={"name": name})


def test_device_register_list_revoke(client: TestClient):
    r = _register(client, "workstation-1")
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    assert key.startswith("eap_d_")
    # 明文 key 即刻可用（设备凭证调用平台）
    r = client.get("/api/v1/agents", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    # 列表可见（明文不可见）
    listing = client.get("/api/v1/auth/devices", headers=HEADERS).json()
    assert any(d["name"] == "workstation-1" for d in listing)
    assert "api_key" not in __import__("json").dumps(listing)
    # 吊销 → 设备 Key 立即失效
    r = client.delete("/api/v1/auth/devices/workstation-1", headers=HEADERS)
    assert r.json()["status"] == "revoked"
    assert client.get("/api/v1/agents",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
    # 吊销后同名可重注册（新明文）
    r = _register(client, "workstation-1")
    assert r.status_code == 200 and r.json()["api_key"] != key


def test_device_duplicate_409(client: TestClient):
    assert _register(client, "dup-device").status_code == 200
    assert _register(client, "dup-device").status_code == 409


def test_device_bad_name_422(client: TestClient):
    assert _register(client, "非法名称!").status_code == 422
    assert _register(client, "").status_code == 422


def test_device_key_disabled_not_authenticable(client: TestClient):
    """吊销即 enabled=False → resolve_tenant 不再命中（未吊销前命中）。"""
    r = _register(client, "lost-laptop")
    key = r.json()["api_key"]
    assert client.get("/api/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    client.delete("/api/v1/auth/devices/lost-laptop", headers=HEADERS)
    assert client.get("/api/v1/models",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401

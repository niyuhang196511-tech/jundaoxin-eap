"""Harness 设备绑定测试（M37）：签发（明文仅一次）/列表/吊销/重名 409/RBAC。

M52-D 可重入：设备名一律 uname 唯一化（设备「在用重名 → 409」，固定名脏库重跑必撞）；
「先建后重名 409」用例首建名唯一、409 用同名重复提交；用毕吊销收尾（设备语义
「先吊销可重用同名」，吊销行不阻塞后续任何运行）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def _register(client: TestClient, name: str):
    return client.post("/api/v1/auth/devices", headers=HEADERS, json={"name": name})


def _revoke(client: TestClient, name: str) -> None:
    """测后自清理：吊销本测试签发的设备（不留在用行，脏库重跑零残留）。"""
    client.delete(f"/api/v1/auth/devices/{name}", headers=HEADERS)


def test_device_register_list_revoke(client: TestClient, uname):
    name = uname("workstation")  # M52-D：唯一名可重入
    r = _register(client, name)
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    assert key.startswith("eap_d_")
    # 明文 key 即刻可用（设备凭证调用平台）
    r = client.get("/api/v1/agents", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    # 列表可见（明文不可见）
    listing = client.get("/api/v1/auth/devices", headers=HEADERS).json()
    assert any(d["name"] == name for d in listing)
    assert "api_key" not in __import__("json").dumps(listing)
    # 吊销 → 设备 Key 立即失效
    r = client.delete(f"/api/v1/auth/devices/{name}", headers=HEADERS)
    assert r.json()["status"] == "revoked"
    assert client.get("/api/v1/agents",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
    # 吊销后同名可重注册（新明文）
    r = _register(client, name)
    assert r.status_code == 200 and r.json()["api_key"] != key
    _revoke(client, name)  # 测后自清理：重注册的在用行同样吊销收尾


def test_device_duplicate_409(client: TestClient, uname):
    name = uname("dup-device")  # M52-D：首建名唯一化，409 用同名重复提交
    assert _register(client, name).status_code == 200
    assert _register(client, name).status_code == 409
    _revoke(client, name)  # 测后自清理


def test_device_bad_name_422(client: TestClient):
    assert _register(client, "非法名称!").status_code == 422
    assert _register(client, "").status_code == 422


def test_device_key_disabled_not_authenticable(client: TestClient, uname):
    """吊销即 enabled=False → resolve_tenant 不再命中（未吊销前命中）。"""
    name = uname("lost-laptop")  # M52-D：唯一名可重入
    r = _register(client, name)
    key = r.json()["api_key"]
    assert client.get("/api/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    client.delete(f"/api/v1/auth/devices/{name}", headers=HEADERS)
    assert client.get("/api/v1/models",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401

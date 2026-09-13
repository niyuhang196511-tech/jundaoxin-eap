"""注册 SDK 生命周期：stop/start/unregister + 热加载（docs/03 §7 完整钩子）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
INVOKE = "/api/v1/agents/faq-agent/invocations"


def test_stop_start_lifecycle(client):
    # stop → on_stop 钩子执行、状态 stopped、调用被拒（503）
    r = client.post("/api/v1/agents/faq-agent/stop", headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    r = client.post(INVOKE, headers=HEADERS, json={"input": "hi"})
    assert r.status_code == 503 and "停用" in r.json()["detail"]
    statuses = {a["name"]: a["status"] for a in client.get("/api/v1/agents", headers=HEADERS).json()}
    assert statuses["faq-agent"] == "stopped"

    # start → 恢复 started，调用恢复
    r = client.post("/api/v1/agents/faq-agent/start", headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "started"
    assert client.post(INVOKE, headers=HEADERS, json={"input": "hi"}).status_code == 200

    # 未知智能体 404
    assert client.post("/api/v1/agents/no-such/stop", headers=HEADERS).status_code == 404
    assert client.post("/api/v1/agents/no-such/start", headers=HEADERS).status_code == 404


def test_unregister_and_hot_reload(client):
    # 注销 faq-agent：目录移除、调用 404
    assert client.delete("/api/v1/agents/faq-agent", headers=HEADERS).status_code == 200
    names = [a["name"] for a in client.get("/api/v1/agents", headers=HEADERS).json()]
    assert "faq-agent" not in names
    assert client.post(INVOKE, headers=HEADERS, json={"input": "hi"}).status_code == 404

    # 热加载：重导入配置模块 → faq-agent 从源码恢复并启动（replace 语义）
    r = client.post("/api/v1/agents/reload", headers=HEADERS)
    assert r.status_code == 200 and r.json()["reloaded_modules"] >= 3
    names = [a["name"] for a in client.get("/api/v1/agents", headers=HEADERS).json()]
    assert "faq-agent" in names
    statuses = {a["name"]: a["status"] for a in client.get("/api/v1/agents", headers=HEADERS).json()}
    assert statuses["faq-agent"] == "started"
    assert client.post(INVOKE, headers=HEADERS, json={"input": "hi"}).status_code == 200

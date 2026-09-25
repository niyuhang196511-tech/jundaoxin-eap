"""M42-A vLLM Provider + multi-LoRA 托管测试（L1 工程部分）：

- VllmProvider：complete 走 OpenAI 兼容形态；health/load/unload 管理端点 URL 与请求体；
  失败路径（HTTP 5xx → ProviderError；健康检查网络错误 → healthy=False 不抛）。
- LoRA 注册 CRUD + RBAC（admin 语义，member JWT 403）。
- load/unload 状态机与失败路径（failed + note 记错误）+ 审计 lora.load/unload。
- vLLM 地址解析：入参显式 base_url > 模型中心 provider=vllm 且模型名匹配的记录。
- 模型路由集成：ModelRecord(provider=vllm) 注册进 capability 链后经 hub 路由到达 vLLM
  供应商（模型名 = LoRA adapter 名）。

HTTP 出站全部注入假 transport（httpx.MockTransport / 假 provider），禁真实网络，离线确定性。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.api.v1 import lora as lora_mod
from eap.db import SessionLocal
from eap.modelhub import vllm as vllm_mod
from eap.modelhub.providers import ProviderError, get_provider
from eap.modelhub.vllm import VllmProvider
from eap.models import AuditLog, LoraAdapter

from .conftest import AUTH
from .test_jwt_auth import _access_token  # noqa: F401 复用 JWT 构造
from .test_oidc import fake_idp  # noqa: F401,F811 复用模拟 IdP 夹具（fixture 再导出）

HEADERS = {**AUTH, "Content-Type": "application/json"}
VLLM_BASE = "http://vllm.local:8000/v1"  # OpenAI 兼容惯例：base_url 含 /v1
VLLM_ROOT = "http://vllm.local:8000"

# ---------- M52-D 可重入命名（样板=test_connector_v2._SFX） ----------
# adapter/模型名跨运行唯一：脏库重跑不撞 LoraAdapter.name 409（EAP-2002）与
# UNIQUE models.name；注册的 provider=vllm 模型用后照旧收尾禁用（路由链不残留高优模型）。
_SFX = uuid.uuid4().hex[:8]

ADAPTER_A = f"m42-adapter-a-{_SFX}"
ADAPTER_A2 = f"m42-adapter-a2-{_SFX}"
SERVED_ALIAS = f"m42-served-alias-{_SFX}"
ADAPTER_B = f"m42-adapter-b-{_SFX}"
ADAPTER_C = f"m42-adapter-c-{_SFX}"
ADAPTER_D = f"m42-adapter-d-{_SFX}"
ADAPTER_DEL = f"m42-adapter-del-{_SFX}"
ADAPTER_JWT = f"m42-adapter-jwt-{_SFX}"
ADAPTER_ROUTER = f"m42-adapter-router-{_SFX}"
MODEL_D = f"m42-vllm-d-{_SFX}"
MODEL_ROUTER = f"m42-vllm-router-{_SFX}"


# ---------- 假 transport / 假 provider（禁真实网络） ----------

def _mock_provider(handler) -> VllmProvider:
    """transport 可注入：httpx.AsyncClient + MockTransport（URL/请求体可断言）。"""
    return VllmProvider(client_factory_fn=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=60.0))


class _FakeVllm:
    """LoRA API 的假 provider：记录调用参数，按 fail 开关决定成败。"""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def load_lora(self, base_url, lora_name, lora_path):
        self.calls.append({"op": "load", "base_url": base_url,
                           "lora_name": lora_name, "lora_path": lora_path})
        if self.fail:
            raise ProviderError("模拟：GPU 显存不足")
        return {"status": "ok"}

    async def unload_lora(self, base_url, lora_name):
        self.calls.append({"op": "unload", "base_url": base_url, "lora_name": lora_name})
        if self.fail:
            raise ProviderError("模拟：卸载失败")
        return {"status": "ok"}

    async def health(self, base_url):
        self.calls.append({"op": "health", "base_url": base_url})
        # served 清单含本运行的唯一 adapter 名（health 端点据此判定 served=True）
        return {"healthy": True, "models": ["base-model", ADAPTER_B]}


def _register_lora(client: TestClient, name: str, **overrides) -> dict:
    payload = {"name": name, "base_model": "Qwen2.5-7B-Instruct",
               "source_path": f"/mnt/c/adapters/{name}", **overrides}
    resp = client.post("/api/v1/lora", headers=HEADERS, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _adapter_status(name: str) -> LoraAdapter:
    with SessionLocal() as db:
        return db.scalar(select(LoraAdapter).where(LoraAdapter.name == name))


# ---------- VllmProvider 单元 ----------

def test_vllm_complete_openai_compat():
    """complete 复用 OpenAI 兼容形态：POST {base}/chat/completions，模型名=adapter 名。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "vllm adapter 回答"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    record = SimpleNamespace(name="m42-adapter-a", remote_model="m42-adapter-a",
                             base_url=VLLM_BASE, api_key=None)
    result = asyncio.run(_mock_provider(handler).complete(
        record=record, messages=[{"role": "user", "content": "你好"}],
        tools=None, temperature=0.3))
    assert result.content == "vllm adapter 回答"
    assert result.tokens_in == 11 and result.tokens_out == 7
    assert result.model == "m42-adapter-a"
    assert calls[0]["url"] == f"{VLLM_BASE}/chat/completions"
    assert calls[0]["body"]["model"] == "m42-adapter-a"
    assert calls[0]["body"]["temperature"] == 0.3


def test_vllm_health_lists_models_and_no_double_v1():
    """health：GET {root}/v1/models；base 含 /v1 后缀时不双拼（两种登记形态均可用）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.method == "GET"
        return httpx.Response(200, json={"object": "list",
                                         "data": [{"id": "base-model"}, {"id": "m42-adapter-a"}]})

    provider = _mock_provider(handler)
    health = asyncio.run(provider.health(VLLM_BASE))
    assert health == {"healthy": True, "models": ["base-model", "m42-adapter-a"]}
    health2 = asyncio.run(provider.health(VLLM_ROOT))  # 不含 /v1 的登记形态
    assert health2["healthy"] is True
    assert seen == [f"{VLLM_ROOT}/v1/models", f"{VLLM_ROOT}/v1/models"]


def test_vllm_health_unreachable_returns_unhealthy():
    """健康检查网络错误不抛异常：healthy=False + error（运维诊断透出）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    health = asyncio.run(_mock_provider(handler).health(VLLM_BASE))
    assert health["healthy"] is False and health["models"] == []
    assert "ConnectError" in health["error"]


def test_vllm_load_unload_urls_and_payload():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url),
                      "body": json.loads(request.content)})
        return httpx.Response(200, json={"status": "ok"})

    provider = _mock_provider(handler)
    loaded = asyncio.run(provider.load_lora(VLLM_BASE, "m42-adapter-a", "/mnt/c/adapters/a"))
    assert loaded == {"status": "ok"}
    unloaded = asyncio.run(provider.unload_lora(VLLM_BASE, "m42-adapter-a"))
    assert unloaded == {"status": "ok"}
    assert calls[0] == {"method": "POST", "url": f"{VLLM_ROOT}/v1/load_lora_adapter",
                        "body": {"lora_name": "m42-adapter-a", "lora_path": "/mnt/c/adapters/a"}}
    assert calls[1] == {"method": "POST", "url": f"{VLLM_ROOT}/v1/unload_lora_adapter",
                        "body": {"lora_name": "m42-adapter-a"}}


def test_vllm_load_http_error_raises_provider_error():
    """load/unload 失败（HTTP 5xx / 网络错误）→ ProviderError（调用方置 failed）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, json={"detail": "CUDA out of memory"})

    provider = _mock_provider(handler)
    with pytest.raises(ProviderError):
        asyncio.run(provider.load_lora(VLLM_BASE, "m42-adapter-a", "/mnt/c/adapters/a"))

    def handler_net_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError):
        asyncio.run(_mock_provider(handler_net_error).unload_lora(VLLM_BASE, "m42-adapter-a"))


def test_get_provider_vllm_registered():
    """providers 注册表：\"vllm\" 映射 VllmProvider。"""
    assert isinstance(get_provider("vllm"), VllmProvider)


# ---------- LoRA 注册 CRUD + RBAC ----------

def test_lora_crud_register_list_delete(client: TestClient):
    view = _register_lora(client, ADAPTER_A)
    assert view["name"] == ADAPTER_A
    assert view["served_as"] == ADAPTER_A, "served_as 默认=name"
    assert view["status"] == "registered"

    # 首建名唯一化后，同名重复提交 → 409（M52-D 守则 3）
    dup = client.post("/api/v1/lora", headers=HEADERS, json={
        "name": ADAPTER_A, "base_model": "x", "source_path": "/x"})
    assert dup.status_code == 409

    bad = client.post("/api/v1/lora", headers=HEADERS, json={
        "name": "Bad Name!", "base_model": "x", "source_path": "/x"})
    assert bad.status_code == 422

    listing = client.get("/api/v1/lora", headers=AUTH).json()
    assert any(a["name"] == ADAPTER_A for a in listing)

    # served_as 显式指定
    view2 = _register_lora(client, ADAPTER_A2, served_as=SERVED_ALIAS)
    assert view2["served_as"] == SERVED_ALIAS

    resp = client.delete(f"/api/v1/lora/{ADAPTER_A2}", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert client.delete(f"/api/v1/lora/{ADAPTER_A2}", headers=AUTH).status_code == 404


def test_lora_delete_blocked_while_loaded(client: TestClient, monkeypatch):
    fake = _FakeVllm()
    monkeypatch.setattr(lora_mod, "get_vllm_provider", lambda: fake)
    _register_lora(client, ADAPTER_DEL)
    with SessionLocal() as db:
        record = db.scalar(select(LoraAdapter).where(LoraAdapter.name == ADAPTER_DEL))
        record.status = "loaded"
        db.commit()
    resp = client.delete(f"/api/v1/lora/{ADAPTER_DEL}", headers=AUTH)
    assert resp.status_code == 409, "加载中的 adapter 必须先 unload 才能删除"
    client.post(f"/api/v1/lora/{ADAPTER_DEL}/unload", headers=HEADERS,
                json={"base_url": VLLM_BASE})
    assert client.delete(f"/api/v1/lora/{ADAPTER_DEL}", headers=AUTH).status_code == 200


def test_lora_rbac_member_403(client: TestClient, fake_idp):  # noqa: F811 参数仅为激活夹具（模块级导入供 pytest 发现）
    """RBAC：member JWT 注册/删除/load → 403（管理面写操作 admin 语义）。"""
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    assert client.post("/api/v1/lora", headers=member, json={
        "name": "m42-denied", "base_model": "x", "source_path": "/x"}).status_code == 403
    assert client.delete(f"/api/v1/lora/{ADAPTER_A}", headers=member).status_code == 403
    assert client.post(f"/api/v1/lora/{ADAPTER_A}/load", headers=member, json={}).status_code == 403
    # admin JWT 放行（可注册成功；名字唯一化 → 脏库重跑不撞 409）
    admin = {"Authorization": f"Bearer {_access_token(roles=['admin'])}",
             "Content-Type": "application/json"}
    resp = client.post("/api/v1/lora", headers=admin, json={
        "name": ADAPTER_JWT, "base_model": "x", "source_path": "/x"})
    assert resp.status_code == 200, resp.text
    # member 只读列表放行（require_api_key 允许 jwt 通道）
    assert client.get("/api/v1/lora", headers=member).status_code == 200


# ---------- load/unload 状态机 + 失败路径 + 审计 ----------

def test_lora_load_unload_state_machine_and_audit(client: TestClient, monkeypatch):
    fake = _FakeVllm()
    monkeypatch.setattr(lora_mod, "get_vllm_provider", lambda: fake)
    _register_lora(client, ADAPTER_B)

    resp = client.post(f"/api/v1/lora/{ADAPTER_B}/load", headers=HEADERS,
                       json={"base_url": VLLM_BASE})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "loaded"
    assert fake.calls[0] == {"op": "load", "base_url": VLLM_BASE,
                             "lora_name": ADAPTER_B,
                             "lora_path": f"/mnt/c/adapters/{ADAPTER_B}"}
    assert _adapter_status(ADAPTER_B).status == "loaded"

    # 审计 lora.load（成功）
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "lora.load",
                                               AuditLog.target == ADAPTER_B))
    assert log is not None and log.detail["ok"] is True and log.detail["base_url"] == VLLM_BASE

    # health 透出：vLLM 服务清单含本 adapter
    health = client.get(f"/api/v1/lora/{ADAPTER_B}/health", headers=AUTH,
                        params={"base_url": VLLM_BASE}).json()
    assert health["healthy"] is True and health["served"] is True
    assert fake.calls[-1]["base_url"] == VLLM_BASE

    resp = client.post(f"/api/v1/lora/{ADAPTER_B}/unload", headers=HEADERS,
                       json={"base_url": VLLM_BASE})
    assert resp.status_code == 200 and resp.json()["status"] == "unloaded"
    assert _adapter_status(ADAPTER_B).status == "unloaded"
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "lora.unload",
                                               AuditLog.target == ADAPTER_B))
    assert log is not None and log.detail["ok"] is True


def test_lora_load_failure_marks_failed(client: TestClient, monkeypatch):
    fake = _FakeVllm(fail=True)
    monkeypatch.setattr(lora_mod, "get_vllm_provider", lambda: fake)
    _register_lora(client, ADAPTER_C)

    resp = client.post(f"/api/v1/lora/{ADAPTER_C}/load", headers=HEADERS,
                       json={"base_url": VLLM_BASE})
    assert resp.status_code == 502
    failed = _adapter_status(ADAPTER_C)
    assert failed.status == "failed" and "显存不足" in failed.note
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "lora.load",
                                               AuditLog.target == ADAPTER_C))
    assert log is not None and log.detail["ok"] is False and "显存不足" in log.detail["error"]

    # 失败后可重试成功 → loaded（状态机允许 failed → loaded 往返）
    fake.fail = False
    resp = client.post(f"/api/v1/lora/{ADAPTER_C}/load", headers=HEADERS,
                       json={"base_url": VLLM_BASE})
    assert resp.status_code == 200 and resp.json()["status"] == "loaded"

    # unload 失败同样置 failed
    fake.fail = True
    resp = client.post(f"/api/v1/lora/{ADAPTER_C}/unload", headers=HEADERS,
                       json={"base_url": VLLM_BASE})
    assert resp.status_code == 502
    assert _adapter_status(ADAPTER_C).status == "failed"


def test_lora_base_url_resolution(client: TestClient, monkeypatch):
    """vLLM 地址解析：显式 base_url > 模型中心 provider=vllm 且模型名匹配记录 > 400。"""
    fake = _FakeVllm()
    monkeypatch.setattr(lora_mod, "get_vllm_provider", lambda: fake)
    _register_lora(client, ADAPTER_D)

    # 无显式 base_url 且模型中心无匹配记录 → 400
    no_addr = client.post(f"/api/v1/lora/{ADAPTER_D}/load", headers=HEADERS, json={})
    assert no_addr.status_code == 400
    assert "base_url" in no_addr.json()["detail"]

    # 模型中心注册 provider=vllm 记录（remote_model = adapter 服务名）
    resp = client.post("/api/v1/models", headers=HEADERS, json={
        "name": MODEL_D, "capabilities": ["chat"], "provider": "vllm",
        "base_url": VLLM_BASE, "remote_model": ADAPTER_D, "priority": 90})
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/v1/lora/{ADAPTER_D}/load", headers=HEADERS, json={})
    assert resp.status_code == 200, resp.text
    assert fake.calls[-1]["base_url"] == VLLM_BASE, "应从模型中心记录解析出 vLLM 地址"

    # 显式 base_url 优先于模型中心解析
    explicit = "http://other-vllm.local:8000/v1"
    client.post(f"/api/v1/lora/{ADAPTER_D}/unload", headers=HEADERS,
                json={"base_url": explicit})
    assert fake.calls[-1]["base_url"] == explicit
    _disable_model(client, MODEL_D)  # 收尾禁用：不污染后续测试的路由链


def _disable_model(client: TestClient, name: str) -> None:
    """把测试注册的 provider=vllm 模型禁用（避免真实网络 fallback 拖慢后续测试）。"""
    resp = client.patch(f"/api/v1/models/{name}", headers=AUTH, params={"enabled": "false"})
    assert resp.status_code == 200, resp.text


# ---------- 模型路由集成：hub.complete → vllm provider ----------

def test_vllm_model_routes_through_hub(client: TestClient, monkeypatch):
    """ModelRecord(provider=vllm) 注册进 capability 链 → /v1/chat/completions 经 vLLM
    供应商到达（模型名 = LoRA adapter 名）；供应商失败自动降级 mock（既有语义不变）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "由 vLLM adapter 回答"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 6}})

    fake_vllm = _mock_provider(handler)
    monkeypatch.setattr(vllm_mod, "_VLLM", fake_vllm)  # get_provider("vllm") 单例注入

    resp = client.post("/api/v1/models", headers=HEADERS, json={
        "name": MODEL_ROUTER, "capabilities": ["chat"], "provider": "vllm",
        "base_url": VLLM_BASE, "remote_model": ADAPTER_ROUTER, "priority": 1})
    assert resp.status_code == 200, resp.text

    resp = client.post("/v1/chat/completions", headers=HEADERS, json={
        "messages": [{"role": "user", "content": "走 vLLM 供应商"}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == MODEL_ROUTER, "应路由到 provider=vllm 的模型（priority=1 置顶）"
    assert "由 vLLM adapter 回答" in data["choices"][0]["message"]["content"]
    assert calls, "调用应到达 vLLM provider"
    assert calls[0]["url"] == f"{VLLM_BASE}/chat/completions"
    assert calls[0]["body"]["model"] == ADAPTER_ROUTER, "请求模型名 = LoRA adapter 服务名"
    _disable_model(client, MODEL_ROUTER)  # 收尾禁用：不污染后续测试的路由链

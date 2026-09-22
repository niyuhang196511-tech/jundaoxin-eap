"""vLLM Provider + multi-LoRA 管理（M42-A，L1 工程部分；docs/20 部署指南）。

vLLM 服务 OpenAI 兼容 API → 补全/流式直接复用 OpenAICompatProvider（继承，零重复）；
差异点在本模块新增的三个管理能力：

- health(base_url)：GET {base}/v1/models —— 列出 vLLM 当前服务的模型/LoRA adapter 名
- load_lora / unload_lora：POST {base}/v1/load_lora_adapter / unload_lora_adapter
  （vLLM ≥0.5 且以 --enable-lora 启动时支持；模型名 = LoRA adapter 名）

base_url 约定：与 OpenAI 兼容惯例一致（含 /v1 后缀，如 http://<wsl-ip>:8000/v1），
chat/completions 走 {base}/chat/completions；管理端点自动剥掉尾部 /v1 再拼
{root}/v1/...，两种登记形态（含或不含 /v1）均可正常工作。

HTTP 出站 transport 可注入（client_factory，测试替换为假件/ MockTransport，零真实网络）。
"""

from __future__ import annotations

import httpx

from .providers import OpenAICompatProvider, ProviderError


def client_factory(timeout: float = 60.0) -> httpx.AsyncClient:
    """HTTP 客户端工厂（模块级函数：测试 monkeypatch 本函数即可离线化）。"""
    return httpx.AsyncClient(timeout=timeout)


class VllmProvider(OpenAICompatProvider):
    """vLLM 供应商：OpenAI 兼容补全（继承）+ 健康检查 + LoRA 动态加载/卸载。"""

    def __init__(self, client_factory_fn=None) -> None:
        # 覆写父类 __init__：经工厂构造客户端（工厂可注入 → transport 可注入）
        self._client = (client_factory_fn or client_factory)()

    @staticmethod
    def _root(base_url: str) -> str:
        """服务根地址：剥尾部 / 与 /v1 后缀（管理端点统一 {root}/v1/...）。"""
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return root

    async def health(self, base_url: str) -> dict:
        """健康检查：GET {base}/v1/models → {healthy, models}（模型/adapter 名列表）。

        网络/HTTP 错误不抛异常——返回 healthy=False + error（透出给运维诊断）。
        """
        try:
            resp = await self._client.get(f"{self._root(base_url)}/v1/models")
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, httpx.StreamError, ValueError) as e:
            return {"healthy": False, "models": [], "error": f"{type(e).__name__}: {e}"}
        models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return {"healthy": True, "models": models}

    async def load_lora(self, base_url: str, lora_name: str, lora_path: str) -> dict:
        """动态加载 LoRA adapter：POST {base}/v1/load_lora_adapter。

        失败（HTTP 4xx/5xx 或网络错误）→ ProviderError（调用方置 failed + 记录错误）。
        """
        return await self._manage(base_url, "/v1/load_lora_adapter",
                                  {"lora_name": lora_name, "lora_path": lora_path})

    async def unload_lora(self, base_url: str, lora_name: str) -> dict:
        """动态卸载 LoRA adapter：POST {base}/v1/unload_lora_adapter。"""
        return await self._manage(base_url, "/v1/unload_lora_adapter",
                                  {"lora_name": lora_name})

    async def _manage(self, base_url: str, path: str, payload: dict) -> dict:
        url = f"{self._root(base_url)}{path}"
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.StreamError) as e:
            raise ProviderError(f"vLLM {path} 失败（{url}）: {e}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {}  # vLLM 部分版本 200 空响应体
        return body if isinstance(body, dict) else {"result": body}


_VLLM = VllmProvider()  # 平台单例（get_provider("vllm") 与 LoRA API 共用）


def get_vllm_provider() -> VllmProvider:
    """取平台 vLLM 单例（测试可 monkeypatch 本函数/ _VLLM 注入假 transport）。"""
    return _VLLM

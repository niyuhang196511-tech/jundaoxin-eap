"""嵌入提供方：hash=离线确定性嵌入（开发/测试，零依赖零网络）；可切 openai。

同步接口（与同步 SQLAlchemy 管道一致）；生产替换为真实 embedding 模型 + Milvus/pgvector
（docs/04 §2、docs/09 选型）。
"""

from __future__ import annotations

import math

import httpx

from .tokenize import tokenize


def fnv1a(s: str) -> int:
    h = 2166136261
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class HashEmbedder:
    """特征哈希嵌入：token 哈希落桶 + 符号位，L2 归一化。确定性、离线、可复现。"""

    provider = "hash"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in tokenize(text):
            h = fnv1a(tok)
            v[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / norm, 6) for x in v]


class OpenAIEmbedder:
    provider = "openai"

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-3-small") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key  # 来自 env 配置（明文），非库内密文
        self.model = model
        self._client = httpx.Client(timeout=30.0)

    def embed(self, text: str) -> list[float]:
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": [text]},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def get_embedder(provider: str, settings):
    if provider == "openai":
        if not (settings.openai_base_url and settings.openai_api_key):
            raise ValueError("openai 嵌入需要配置 EAP_OPENAI_BASE_URL / EAP_OPENAI_API_KEY")
        return OpenAIEmbedder(settings.openai_base_url, settings.openai_api_key)
    return HashEmbedder(dim=settings.embed_dim)

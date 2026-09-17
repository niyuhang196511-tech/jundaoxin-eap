"""向量存储抽象（docs/04 §1 三路索引之向量路）：

- 默认：SQLite 内嵌向量 + 本地余弦（离线零依赖）
- 生产：EAP_MILVUS_URI 配置后走 Milvus（单集合 + kb 过滤；MetricType=IP，向量需归一化
  ——hash/openai 嵌入归一化语义见 embedding.py）；Milvus 不可达时自动回退本地余弦并告警。

Milvus 集合 schema（eap_vectors）：
    chunk_id Int64 主键 | kb_id Int64 | vector FloatVector(dim)
配合 pymilvus MilvusClient（2.4+）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("eap.milvus")

_COLLECTION = "eap_vectors"


class LocalVectorStore:
    """本地余弦（默认）：向量在 Chunk.embedding。

    M12 提速：numpy 矩阵化一次算全部余弦（替代逐条 Python 循环，数千块快两个数量级）；
    numpy 未安装回退逐条实现。O(N) 扫描语义不变（生产大规模切 Milvus）。

    M15 跨请求缓存：矩阵按 (kb_id, chunk 数, 最后 chunk id) 做失效键缓存在进程内——
    摄入/删除改变 chunk 集合时键不匹配即自动重建（无需显式失效通知）。
    """

    _cache: dict[int, tuple[tuple, object, list]] = {}  # kb_id -> (失效键, 归一化矩阵, chunk_id 序)

    def scores_for(self, chunks, query_vec) -> list[float]:
        try:
            import numpy as np
        except ImportError:
            from .embedding import cosine

            return [cosine(query_vec, c.embedding or []) for c in chunks]

        q = np.asarray(query_vec, dtype="float32")
        q_norm = float(np.linalg.norm(q)) or 1.0
        if not chunks:
            return []

        kb_id = chunks[0].kb_id
        cache_key = (len(chunks), chunks[-1].id, chunks[0].id)
        cached = self._cache.get(kb_id)
        if cached is not None and cached[0] == cache_key:
            _, matrix, chunk_ids = cached
        else:
            rows = []
            ids = []
            for c in chunks:
                emb = np.asarray(c.embedding or [], dtype="float32")
                if emb.size == 0:
                    continue
                rows.append(emb / (float(np.linalg.norm(emb)) or 1.0))
                ids.append(c.id)
            if not rows:
                return [0.0] * len(chunks)
            matrix = np.vstack(rows)
            chunk_ids = ids
            self._cache[kb_id] = (cache_key, matrix, chunk_ids)
            if len(self._cache) > 64:  # 防无界增长：超出即全清（下次检索重建）
                self._cache.clear()

        # 查询按缓存矩阵的 chunk 序对齐（chunks 顺序 = 矩阵行序）
        qn = q / q_norm
        scores_matrix = matrix @ qn
        by_id = {cid: round(float(s), 6) for cid, s in zip(chunk_ids, scores_matrix)}
        return [by_id.get(c.id, 0.0) for c in chunks]

    def upsert(self, kb_id: int, chunk_id: int, vector: list[float]) -> None:
        self._cache.pop(kb_id, None)  # 摄入即失效（下次检索重建）

    def delete(self, chunk_ids: list[int]) -> None:
        pass  # 删除以 chunk 数/尾 id 变化体现，键失配自然重建


class MilvusVectorStore:
    def __init__(self, uri: str, dim: int) -> None:
        from pymilvus import MilvusClient

        self._client = MilvusClient(uri=uri)
        self._dim = dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self._client.has_collection(_COLLECTION):
            from pymilvus import DataType

            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.INT64, is_primary=True)
            schema.add_field("kb_id", DataType.INT64)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
            index_params = self._client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="FLAT",
                                   metric_type="IP", params={})
            # Strong 一致性：摄入后立即可检索（规模小，代价可接受；生产可按需降级）
            self._client.create_collection(_COLLECTION, schema=schema,
                                           index_params=index_params,
                                           consistency_level="Strong")

    def scores_for(self, chunks, query_vec) -> list[float]:
        """从 Milvus 搜索（全部 kb_id 过滤在调用方无需——传入的 chunks 已是本 KB），
        把命中 chunk_id 的相似度映射回传入 chunks 的位置；Milvus 缺失的块回退本地余弦。"""
        kb_id = chunks[0].kb_id if chunks else 0
        try:
            results = self._client.search(
                _COLLECTION, data=[query_vec], limit=min(60, max(len(chunks), 1)),
                filter=f"kb_id == {kb_id}", output_fields=["chunk_id"])
        except Exception as e:
            logger.warning("Milvus 检索失败，回退本地余弦: %s", e)
            return LocalVectorStore().scores_for(chunks, query_vec)

        from .embedding import cosine

        by_id = {r["entity"].get("chunk_id"): r["distance"]
                 for batch in results for r in batch}
        out = []
        for c in chunks:
            if c.id in by_id:
                out.append(by_id[c.id])
            else:
                out.append(cosine(query_vec, c.embedding or []))
        return out

    def upsert(self, kb_id: int, chunk_id: int, vector: list[float]) -> None:
        try:
            self._client.upsert(_COLLECTION, {"chunk_id": chunk_id, "kb_id": kb_id,
                                              "vector": vector})
        except Exception as e:
            logger.warning("Milvus upsert 失败（chunk %s）: %s", chunk_id, e)

    def delete(self, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        try:
            ids = ",".join(str(i) for i in chunk_ids)
            self._client.delete(_COLLECTION, filter=f"chunk_id in [{ids}]")
        except Exception as e:
            logger.warning("Milvus delete 失败: %s", e)


_STORE = None  # (uri_key, store)：按配置键缓存，同进程内配置切换（测试/多 KB）不串


def get_vector_store(settings) -> LocalVectorStore | MilvusVectorStore:
    """进程级单例（按 milvus_uri 键）：EAP_MILVUS_URI 配置即用 Milvus；
    连接失败回退本地并记录（后续同键请求不再重试，避免每次请求都打挂掉的连接）。"""
    global _STORE
    key = settings.milvus_uri or "local"
    if _STORE is not None and _STORE[0] == key:
        return _STORE[1]
    if settings.milvus_uri:
        try:
            store = MilvusVectorStore(settings.milvus_uri, settings.embed_dim)
            _STORE = (key, store)
            return store
        except Exception as e:
            logger.warning("Milvus 连接失败，向量路回退本地余弦: %s", e)
            _STORE = (key, LocalVectorStore())
            return _STORE[1]
    _STORE = (key, LocalVectorStore())
    return _STORE[1]

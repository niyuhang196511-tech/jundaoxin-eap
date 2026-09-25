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

    M15 跨请求缓存 + M17 持久化：矩阵按 (kb_id, chunk 数, 首/尾 chunk id) 做失效键缓存在
    进程内，并落盘 EAP_MEDIA_DIR/../vector-cache/{kb_id}.npz（矩阵+chunk id 序）——进程重启
    后首次检索直接加载，仅当 chunk 集合变化时重建。

    M53-A 磁盘缓存内容指纹（跨库碰撞真 bug 修复，M52-D 批次实测登记）：
    - 碰撞根因：磁盘层旧失效键 (chunk 数, 首/尾 chunk id) 全是**每库各自的自增 id**，且
      npz 文件名只有 kb_id。换库（EAP_TEST_DB_URL 切换）或同路径重建库（rm db 重新 init）
      时 id 序列完全复现，磁盘上的旧库 npz 键恰好匹配 → 加载**别的库的向量矩阵**，余弦分
      张冠李戴 → 融合排序头部翻转（实证：stale 缓存下 test_rerank 长文/短文头名互换）。
      进程内缓存不受此害（单进程单库，且摄入 upsert 即失效），故仅升级磁盘层校验。
    - 指纹方案：加载/落盘前对**当前库实算**内容指纹 sha256（版本前缀 + kb_id + chunk 数 +
      嵌入配置身份 + 全部 chunk 有序 (id, 嵌入维度, content 原文)），存入 npz 内部
      fingerprint 字段；加载时与实算值比对，不一致 → 视为 miss 重建。chunks 为调用方已
      载入的 ORM 对象，指纹零额外查询、O(chunk 总字节) 一次哈希，且仅在进程内缓存 miss
      时计算（每进程每库至多一次，热路径查询零开销）。
    - 旧格式 npz（无 fingerprint 字段，M53-A 之前落盘）一律视为 miss 重建——向后兼容
      即安全失效；重建后以新格式原子覆写（tmp + os.replace，防半截文件）。
    - 诚实局限：指纹覆盖 id/文本/维度/全局嵌入配置，不含嵌入向量数值本身——同 id 同文本
      同维度同配置下若嵌入值仍不同（KB 级 embedding_provider 覆盖切换、嵌入服务对同一
      文本非确定输出、绕过平台直改 DB 的 embedding 列），磁盘缓存无法察觉。默认 hash
      嵌入为纯函数（文本+维度 → 向量确定），该盲区实际不可达；切 openai 嵌入的库若改
      KB 级 provider，建议手动清理 vector-cache/。Milvus 路径不经此缓存，不受影响。
    """

    _cache: dict[int, tuple[tuple, object, list]] = {}  # kb_id -> (失效键, 归一化矩阵, chunk_id 序)

    def _fingerprint(self, kb_id: int, chunks) -> str:
        """当前库内容指纹：sha256(版本前缀:kb_id:chunk 数:嵌入配置: 有序 (id, 嵌入维度, 原文))。

        取代旧 (chunk 数, 首/尾 id) 键做磁盘校验——id 序列跨库/重建复现时旧键必撞，
        内容原文入摘要后「同键不同内容」必然失配（见类 docstring M53-A）。
        """
        import hashlib

        from ..config import get_settings

        settings = get_settings()
        h = hashlib.sha256()
        h.update(f"eap-vecfp-v1:{kb_id}:{len(chunks)}"
                 f":{settings.embedding_provider}:{settings.embed_dim}".encode())
        for c in chunks:
            h.update(f":{c.id}:{len(c.embedding or ())}".encode())
            h.update((c.content or "").encode("utf-8", "replace"))
        return h.hexdigest()

    def _persisted(self, kb_id: int, fingerprint: str):
        """从磁盘 npz 加载（存在且内容指纹与当前库实算值一致才命中）。

        M53-A：无 fingerprint 字段的旧格式 npz 一律 miss（安全失效）；指纹不符
        （跨库 id 复现 / 同路径重建内容变化）同样 miss，由调用方重建。
        """
        import os

        import numpy as np

        from ..config import get_settings

        # abspath 词法归一（与 _persist 同构）：POSIX 对未归一的 media_dir/../ 按文件系统
        # 解析——media 目录不存在时 exists() 恒 False、磁盘缓存永不命中（Win32 词法折叠
        # ".." 掩盖该缺陷：本地绿/CI ubuntu 红，run #36 容器 A/B 实证）
        path = os.path.abspath(os.path.join(get_settings().media_dir, "..", "vector-cache", f"{kb_id}.npz"))
        if not os.path.exists(path):
            return None
        try:
            with np.load(path) as data:
                if "fingerprint" not in data.files:
                    return None  # 旧格式（仅 cache_key 校验）：视为 miss 重建
                if str(data["fingerprint"]) != fingerprint:
                    return None  # 内容指纹不符：磁盘是别的库/旧内容的矩阵
                return data["matrix"], data["chunk_ids"].tolist()
        except Exception:
            return None

    def _persist(self, kb_id: int, fingerprint: str, matrix, chunk_ids: list) -> None:
        """原子落盘（M53-A）：先写同目录 pid 命名 tmp 再 os.replace，防崩溃留半截文件。"""
        import os

        import numpy as np

        from ..config import get_settings

        cache_dir = os.path.abspath(os.path.join(get_settings().media_dir, "..", "vector-cache"))
        tmp = os.path.join(cache_dir, f".{kb_id}.{os.getpid()}.tmp.npz")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            np.savez_compressed(
                tmp,
                matrix=matrix, chunk_ids=np.asarray(chunk_ids, dtype="int64"),
                fingerprint=np.asarray(fingerprint),
            )
            os.replace(tmp, os.path.join(cache_dir, f"{kb_id}.npz"))
        except Exception as e:
            import logging

            logging.getLogger("eap.vector").warning("向量索引持久化失败: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass

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
            # M53-A：磁盘层以内容指纹校验（进程内热路径仍用轻量键，单进程单库语义不变）
            fingerprint = self._fingerprint(kb_id, chunks)
            loaded = self._persisted(kb_id, fingerprint)
            if loaded is not None:
                matrix, chunk_ids = loaded
                self._cache[kb_id] = (cache_key, matrix, chunk_ids)
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
                self._persist(kb_id, fingerprint, matrix, chunk_ids)
                if len(self._cache) > 64:  # 防无界增长：超出即全清（下次检索重建）
                    self._cache.clear()

        # 查询按缓存矩阵的 chunk 序对齐（chunks 顺序 = 矩阵行序）
        qn = q / q_norm
        scores_matrix = matrix @ qn
        by_id = {cid: round(float(s), 6) for cid, s in zip(chunk_ids, scores_matrix)}
        return [by_id.get(c.id, 0.0) for c in chunks]

    def upsert(self, kb_id: int, chunk_id: int, vector: list[float]) -> None:
        self._cache.pop(kb_id, None)  # 摄入即失效（下次检索重建+重落盘）

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

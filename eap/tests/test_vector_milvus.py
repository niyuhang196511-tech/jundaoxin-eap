"""Milvus 向量路集成测试（docs/04 §1 生产形态）。

需要 Milvus 可达：默认 localhost:19530（compose standalone），可用 EAP_TEST_MILVUS_URI 覆盖。
不可达时整文件 skip——离线回归不受影响。
"""

from __future__ import annotations

import os
import socket

import pytest

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}
MILVUS_URI = os.environ.get("EAP_TEST_MILVUS_URI", "http://localhost:19530")


def _milvus_alive() -> bool:
    try:
        with socket.create_connection(("localhost", 19530), timeout=1):
            return True
    except OSError:
        return False


pytest.importorskip("pymilvus", reason="pymilvus 未安装（uv sync --extra milvus）")

pytestmark = pytest.mark.skipif(
    not _milvus_alive(), reason="本机 Milvus 不可达（docker compose up -d milvus 启动）")


@pytest.fixture()
def milvus_client():
    """带 EAP_MILVUS_URI 的独立应用实例（向量路上行/检索走 Milvus）。

    测试前重建集合：保证 Strong 一致性设置生效且无历史脏数据。
    """
    from eap.config import get_settings

    os.environ["EAP_MILVUS_URI"] = MILVUS_URI
    get_settings.cache_clear()
    try:
        from pymilvus import MilvusClient

        mc = MilvusClient(uri=MILVUS_URI)
        if mc.has_collection("eap_vectors"):
            mc.drop_collection("eap_vectors")
        mc.close()
    except Exception:
        pass
    from eap.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c
    os.environ.pop("EAP_MILVUS_URI", None)
    get_settings.cache_clear()


def test_milvus_vector_path_end_to_end(client, milvus_client):
    from pymilvus import MilvusClient

    kb = client.post("/api/v1/kb", headers=HEADERS,
                     json={"name": "milvus-kb", "title": "milvus-kb", "template": "doc"}).json()
    kb_id = next(k["id"] for k in client.get("/api/v1/kb", headers=HEADERS).json()
                 if k["name"] == "milvus-kb")
    try:
        # 摄入 → 向量上行 Milvus（单例 store 已建集合）
        r = client.post(f"/api/v1/kb/milvus-kb/documents", headers=HEADERS,
                        json={"title": "milvus-doc", "text": "EAP一体机 支持顺丰次日达配送。"})
        assert r.status_code == 200, r.text

        mc = MilvusClient(uri=MILVUS_URI)
        assert mc.has_collection("eap_vectors")
        rows = mc.query("eap_vectors", filter=f"kb_id == {kb_id}",
                        output_fields=["chunk_id"])
        assert rows, "Milvus 中应有该 KB 的向量"

        # 检索走 Milvus 相似度（路径无崩溃且命中）
        hits = client.post(f"/api/v1/kb/milvus-kb/retrieve", headers=HEADERS,
                           json={"query": "EAP一体机 配送", "top_k": 3}).json()["hits"]
        assert hits and hits[0]["citation"]["document"] == "milvus-doc"

        # 级联删除 → Milvus 中向量同步清理
        doc_id = client.get(f"/api/v1/kb/milvus-kb/documents", headers=HEADERS).json()[0]["id"]
        client.delete(f"/api/v1/kb/milvus-kb/documents/{doc_id}", headers=HEADERS)
        rows = mc.query("eap_vectors", filter=f"kb_id == {kb_id}", output_fields=["chunk_id"])
        assert rows == []
        mc.close()
    except Exception:
        raise
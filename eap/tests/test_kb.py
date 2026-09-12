"""知识中心测试：摄入/分块/检索/Citation/级联删除。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH


def test_chunking():
    from eap.knowledge.chunking import split_text

    text = "\n\n".join([f"段落{i}，" + "内容" * 400 for i in range(3)])
    chunks = split_text(text, target=400, overlap=60)
    assert len(chunks) >= 4
    assert all(len(c) <= 700 for c in chunks)
    assert chunks[0] != chunks[1]


def test_kb_lifecycle_and_retrieval(client: TestClient):
    # 创建 KB
    resp = client.post("/api/v1/kb", headers=AUTH,
                       json={"name": "test-kb", "title": "测试库", "template": "doc"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-kb"

    # 重名 409
    assert client.post("/api/v1/kb", headers=AUTH,
                       json={"name": "test-kb"}).status_code == 409

    # 摄入文档
    resp = client.post("/api/v1/kb/test-kb/documents", headers=AUTH, json={
        "title": "退货政策",
        "text": "自签收之日起 7 天内可无理由退货，需保留完整包装。\n\n"
                "生鲜类商品不支持无理由退货。质量问题 15 天内可退。",
    })
    assert resp.status_code == 200
    doc_id = resp.json()["document_id"]

    # 混合检索 + Citation
    resp = client.post("/api/v1/kb/test-kb/retrieve", headers=AUTH,
                       json={"query": "生鲜能不能退货", "top_k": 3})
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert hits, "应检索到内容"
    assert hits[0]["citation"]["document"] == "退货政策"
    assert "生鲜" in hits[0]["content"]

    # 级联删除
    resp = client.delete(f"/api/v1/kb/test-kb/documents/{doc_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["deleted_chunks"] >= 1
    resp = client.post("/api/v1/kb/test-kb/retrieve", headers=AUTH,
                       json={"query": "退货", "top_k": 3})
    assert resp.json()["hits"] == []


def test_faq_ingest_and_retrieve(client: TestClient):
    resp = client.post("/api/v1/kb/website-faq/retrieve", headers=AUTH,
                       json={"query": "手写的智能体怎么注册", "top_k": 2})
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert hits, "种子 FAQ 应命中"
    assert "register_agent" in hits[0]["content"] or "注册" in hits[0]["content"]
    assert hits[0]["citation"]["kb"] == "website-faq"


def test_retrieve_auth_required(client: TestClient):
    assert client.post("/api/v1/kb/website-faq/retrieve",
                       json={"query": "x"}).status_code == 401

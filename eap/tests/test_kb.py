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


def test_document_upload_async_ingest(client: TestClient):
    """M12 文件上传：PDF/DOCX 解析经任务引擎异步摄入（用 txt 验证全链路，解析器单测覆盖格式分发）。"""
    import io
    import time

    files = {"file": ("notes.txt", io.BytesIO("第一段内容。\n\n第二段内容。".encode()), "text/plain")}
    r = client.post("/api/v1/kb/website-faq/upload", headers=AUTH, files=files,
                    data={"title": "上传测试"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).json()
        if detail["state"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.2)
    assert detail["state"] == "COMPLETED", detail
    assert detail["result"]["chunks"] >= 1

    # 文档出现在列表
    docs = client.get("/api/v1/kb/website-faq/documents", headers=AUTH).json()
    assert any(d["title"] == "上传测试" for d in docs)


def test_parser_format_dispatch():
    """解析器分发：txt 直读；未知格式提示性报错。"""
    import pytest

    from eap.knowledge.parsers import extract_text

    assert "内容" in extract_text("a.txt", "内容".encode())
    with pytest.raises(ValueError, match="不支持的文档格式"):
        extract_text("a.exe", b"MZ")

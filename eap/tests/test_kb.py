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

    from eap.knowledge import parsers

    def extract_text(fn, data):
        return parsers.extract_text(fn, data, parser="local")

    assert "内容" in extract_text("a.txt", "内容".encode())
    with pytest.raises(ValueError, match="不支持的文档格式"):
        extract_text("a.exe", b"MZ")


def test_mineru_parser_flow(monkeypatch):
    """MinerU 云端流：申请链接 → PUT → 建任务 → 轮询 → zip 取 md（HTTP 全 mock）。"""
    import io
    import zipfile

    from eap.config import get_settings
    from eap.knowledge import parsers

    monkeypatch.setenv("EAP_MINERU_TOKEN", "test-token")
    get_settings.cache_clear()
    try:
        calls = []

        def fake_post(url, **kw):
            calls.append(("POST", url))
            if url.endswith("/file-urls/batch"):
                return _resp(200, {"data": {"batch_id": "b-1",
                                            "file_urls": ["https://oss/upload/1"]}})
            if url.endswith("/extract/task/batch"):
                return _resp(200, {"data": {}})
            raise AssertionError(url)

        def fake_put(url, **kw):
            calls.append(("PUT", url))
            return _resp(200, {})

        def fake_get(url, **kw):
            calls.append(("GET", url))
            if url.endswith("/batch/b-1"):
                return _resp(200, {"data": {"extract_result": [
                    {"state": "done", "full_zip_url": "https://oss/result.zip"}]}})
            if url.endswith("result.zip"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr("out.md", "# 标题\n\nMinerU 解析结果")
                return _resp(200, buf.getvalue())
            raise AssertionError(url)

        parsers.httpx.post = fake_post
        parsers.httpx.put = fake_put
        parsers.httpx.get = fake_get
        # extract_text 内部 time.sleep(3) 轮询——首查即 done 不会触发
        md = parsers.extract_text("scan.pdf", b"%PDF-fake", parser="mineru_cloud")
        assert md.startswith("# 标题")
        assert any(u[1].endswith("/file-urls/batch") for u in calls)
    finally:
        get_settings.cache_clear()


def test_mineru_selfhosted_flow(monkeypatch):
    """自托管 /file_parse：单次 POST 直接取 md_content。"""
    from eap.config import get_settings
    from eap.knowledge import parsers

    monkeypatch.setenv("EAP_MINERU_TOKEN", "t")
    monkeypatch.setattr(get_settings(), "mineru_api_url", "http://mineru.local", raising=False)
    get_settings.cache_clear()
    try:
        parsers.httpx.post = lambda url, **kw: _resp(
            200, [{"md_content": "# 自托管结果"}]) if url.endswith("/file_parse") else None
        md = parsers.extract_text("doc.pdf", b"%PDF", parser="mineru_selfhosted")
        assert md == "# 自托管结果"
    finally:
        get_settings.cache_clear()


def _resp(status, payload):
    class R:
        def __init__(self):
            self.status_code = status

        def raise_for_status(self):
            assert self.status_code < 400, self.status_code

        def json(self):
            return payload

        @property
        def text(self):
            import json

            return json.dumps(payload)

        @property
        def content(self):
            if isinstance(payload, bytes):
                return payload
            return str(payload).encode()

    return R()

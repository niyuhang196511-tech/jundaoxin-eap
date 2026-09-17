"""文档解析（M12/M14）：多后端可插拔——内置本地解析 + MinerU（扫描件/复杂版式 → Markdown）。

后端注册表（parser registry）：
- `local`（默认）：PDF 文本层（pypdf）/ DOCX（python-docx）/ TXT·MD 直读
- `mineru_cloud`：mineru.net 开放 API（申请批处理上传链接 → 上传 → 轮询 → 下载 zip 取 .md），
  支持 OCR 与表格公式，适合扫描件/论文/报告
- `mineru_selfhosted`：自托管 mineru-api 的 /file_parse（单次 POST 直接返回 markdown）

选择：上传端点按 `parser` 参数指定（默认取 EAP_DOCS_PARSER 配置）；后端未安装依赖或
MinerU 未配置时给出提示性错误。HTTP 全部收敛在 _http_* 注入点（测试可 mock）。
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings

logger = logging.getLogger("eap.parsers")

_PARSERS: dict[str, callable] = {}


def register_parser(name: str, fn) -> None:
    """注册解析后端：fn(filename, data) -> str（markdown/纯文本）。"""
    _PARSERS[name] = fn


def available_parsers() -> list[str]:
    settings = get_settings()
    out = ["local"]
    if settings.mineru_token:
        out.append("mineru_cloud")
    if settings.mineru_api_url:
        out.append("mineru_selfhosted")
    return sorted(set(out) | set(_PARSERS))


def extract_text(filename: str, data: bytes, parser: str | None = None) -> str:
    """按后端解析文件字节为文本/Markdown。parser 空 = EAP_DOCS_PARSER（默认 local）。"""
    backend = parser or get_settings().docs_parser or "local"
    fn = _PARSERS.get(backend)
    if fn is None:
        raise ValueError(f"未知解析后端 {backend!r}（可用: {', '.join(available_parsers())}）")
    return fn(filename, data)


# ---------- local 内置后端 ----------

def _local(filename: str, data: bytes) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "pdf":
        return _pdf_local(data)
    if suffix == "docx":
        return _docx_local(data)
    if suffix in ("txt", "md", "markdown"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"local 后端不支持的文档格式 .{suffix}（支持 pdf / docx / txt / md；"
                     "扫描件/复杂版式请用 mineru 后端）")


def _pdf_local(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ValueError("PDF 解析需要 pypdf：uv sync --extra docs") from e
    import io

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(p for p in pages if p.strip())


def _docx_local(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise ValueError("DOCX 解析需要 python-docx：uv sync --extra docs") from e
    import io

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


# ---------- MinerU 后端 ----------

def _mineru_settings() -> tuple[str, str]:
    s = get_settings()
    token = s.mineru_token
    if not token:
        raise ValueError("MinerU 云端解析需要配置 EAP_MINERU_TOKEN")
    return s.mineru_api_url.rstrip("/"), token


def _mineru_cloud(filename: str, data: bytes) -> str:
    """mineru.net 批处理流：申请上传链接 → PUT 文件 → 建批任务 → 轮询 → 下载 zip 取 .md。"""
    import io
    import time
    import zipfile


    base, token = _mineru_settings()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        # ① 申请上传链接
        resp = httpx.post(f"{base}/file-urls/batch",
                          json={"enable_formula": True, "enable_table": True,
                                "language": "ch", "files": [{"name": filename,
                                                             "is_ocr": True}]},
                          headers=headers, timeout=30)
        resp.raise_for_status()
        batch_id = resp.json()["data"]["batch_id"]
        upload_url = resp.json()["data"]["file_urls"][0]

        # ② PUT 文件（对象存储直传）
        put = httpx.put(upload_url, content=data, timeout=120)
        put.raise_for_status()

        # ③ 建批提取任务
        resp = httpx.post(f"{base}/extract/task/batch",
                          json={"batch_id": batch_id, "enable_formula": True,
                                "enable_table": True, "language": "ch"},
                          headers=headers, timeout=30)
        resp.raise_for_status()

        # ④ 轮询批结果（上限 5 分钟）
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = httpx.get(f"{base}/extract/task/batch/{batch_id}",
                              headers=headers, timeout=30).json()
            results = state["data"]["extract_result"]
            done = [r for r in results if r.get("state") == "done"]
            if done:
                zip_url = done[0]["full_zip_url"]
                break
            failed = [r for r in results if r.get("state") == "failed"]
            if failed:
                raise ValueError(f"MinerU 解析失败: {failed[0].get('err_msg', '未知')}")
            time.sleep(3)
        else:
            raise ValueError("MinerU 解析超时（5 分钟）")

        # ⑤ 下载 zip，取第一个 .md
        archive = httpx.get(zip_url, timeout=120)
        archive.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
            md_names = [n for n in zf.namelist() if n.endswith(".md")]
            if not md_names:
                raise ValueError("MinerU 结果包中没有 .md")
            return zf.read(md_names[0]).decode("utf-8", errors="replace")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"MinerU 云端解析失败: {e}") from e


def _mineru_selfhosted(filename: str, data: bytes) -> str:
    """自托管 mineru-api：POST /file_parse 单次调用直接返回 markdown。"""

    base, token = _mineru_settings()
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/file_parse",
            files={"files": (filename, data)},
            data={"output_format": "md", "backend": "pipeline"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload if isinstance(payload, list) else payload.get("results", [payload])
        md = results[0].get("md_content") or ""
        if not md.strip():
            raise ValueError("解析结果为空")
        return md
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"MinerU 自托管解析失败: {e}") from e


register_parser("local", _local)
register_parser("mineru_cloud", _mineru_cloud)
register_parser("mineru_selfhosted", _mineru_selfhosted)

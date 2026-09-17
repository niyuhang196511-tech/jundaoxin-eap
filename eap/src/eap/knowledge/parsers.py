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
import os

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
    """按后端解析文件字节为文本/Markdown。parser 空 = EAP_DOCS_PARSER（默认 local）。

    M16 图片链路：解析产出 md 后，若 EAP_VISION_MODEL 配置了视觉模型，对文档内图片
    逐张生成中文描述并插入图片引用之后——图片内容进入 chunk 文本，变得可检索。
    """
    backend = parser or get_settings().docs_parser or "local"
    fn = _PARSERS.get(backend)
    if fn is None:
        raise ValueError(f"未知解析后端 {backend!r}（可用: {', '.join(available_parsers())}）")
    md = fn(filename, data)
    from .media import extract_image_refs

    if get_settings().vision_model and extract_image_refs(md):
        md = _caption_images(md)
    return md


def _caption_images(md: str) -> str:
    """对 md 中每张 /media 图片调用视觉模型生成描述，插入引用后（失败跳过单张）。"""
    import re

    from .media import image_abs_path

    out = md
    for m in re.finditer(r"(!\[[^\]]*\]\((/media/[^)]+)\))", md):
        ref, web = m.group(1), m.group(2)
        path = image_abs_path(web)
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                desc = _vision_describe(f.read(), os.path.splitext(path)[1])
        except Exception as e:
            logger.warning("图片描述失败 %s: %s", web, e)
            continue
        if desc:
            out = out.replace(ref, f"{ref}\n\n【图片描述】{desc}", 1)
    return out


def _vision_describe(data: bytes, suffix: str) -> str | None:
    """视觉模型生成中文图片描述（OpenAI 兼容 chat/completions，图片走 data URL）。"""
    import base64

    s = get_settings()
    if not (s.openai_base_url and s.vision_model):
        return None
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
        suffix.lstrip(".").lower(), "image/png")
    b64 = base64.b64encode(data).decode()
    resp = httpx.post(
        f"{s.openai_base_url.rstrip('/')}/chat/completions",
        json={"model": s.vision_model, "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "用一句简洁的中文描述这张图片的内容，供知识检索使用。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }], "temperature": 0.2},
        headers={"Authorization": f"Bearer {s.openai_api_key or ''}"},
        timeout=60,
    )
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"].get("content") or "").strip() or None


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
    md = "\n\n".join(p for p in pages if p.strip())
    # M16 图片：内嵌图（照片/图表）落盘并附引用（视觉模型启用后自动补描述）
    if get_settings().docs_extract_images:
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                for img in page.images:
                    web = _save_image_safe(img.data, img.name)
                    if web:
                        parts.append(f"![第{i + 1}页图片]({web})")
            except Exception:
                continue  # 单页图片提取失败不影响文本
        if parts:
            md = (md + "\n\n" if md.strip() else "") + "\n\n".join(parts)
    return md


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
    md = "\n".join(parts)
    # M16 图片：内嵌图（inline shapes）落盘并附引用
    if get_settings().docs_extract_images:
        image_parts = []
        for i, shape in enumerate(document.inline_shapes):
            try:
                rId = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
                blob = document.part.related_parts[rId].blob
                web = _save_image_safe(blob, f"docx-{i}.png")
                if web:
                    image_parts.append(f"![文档图片{i + 1}]({web})")
            except Exception:
                continue
        if image_parts:
            md = (md + "\n\n" if md.strip() else "") + "\n\n".join(image_parts)
    return md


def _save_image_safe(data: bytes, name: str) -> str | None:
    """图片字节落盘（/media 路径）；过小（<1KB，多为装饰元素）或失败返回 None。"""
    if not data or len(data) < 1024:
        return None
    try:
        from .media import save_image

        return save_image(data, os.path.splitext(name)[1] or ".png")
    except Exception as e:
        logger.warning("图片落盘失败 %s: %s", name, e)
        return None


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
            md = zf.read(md_names[0]).decode("utf-8", errors="replace")
            # M16 图片：结果包的 images/ 落盘并重写引用（视觉模型启用后由 extract_text 补描述）
            if get_settings().docs_extract_images:
                from .media import save_and_rewrite

                images = {n: zf.read(n) for n in zf.namelist()
                          if n.startswith("images/") and len(zf.read(n)) >= 1024}
                md = save_and_rewrite(md, images)
            return md
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

"""图片媒体管理（M16）：文档内嵌图片的落盘与服务。

- 存储：EAP_MEDIA_DIR（默认 ./media）下按内容哈希分目录（同图天然去重）
- 服务：main.py 将 EAP_MEDIA_DIR 挂载为 /media 静态目录，chunk 里的图片引用可直接访问
- 密钥/隐私：媒体目录属知识库资产，纳入备份脚本范围（与库同生命周期）
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re


def media_root() -> str:
    from ..config import get_settings

    root = get_settings().media_dir
    os.makedirs(root, exist_ok=True)
    return root


def _digest_dir(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def save_image(data: bytes, suffix: str = ".png") -> str:
    """图片字节 → 落盘，返回 web 路径（/media/<hash>/image<suffix>）。同内容去重。

    suffix 来自解析产物中的图片文件名（不可信）：仅接受 .字母数字 形式，
    其余一律归一化为 .png；落盘路径规范化后必须位于 media 根目录之内（防穿越）。
    """
    import re

    suffix = suffix if suffix.startswith(".") else "." + suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        suffix = ".png"
    root = Path(media_root()).resolve()
    digest = _digest_dir(data)
    target = (root / digest / f"image{suffix}").resolve()
    if not target.is_relative_to(root):
        raise ValueError("媒体路径越界，已拒绝")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return f"/media/{digest}/image{suffix}"


_IMG_RE = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")


def rewrite_local_links(md: str) -> str:
    """把 markdown 里的相对图片链接（MinerU 的 images/xxx.jpg、本地图路径）重写为 /media web 路径。

    约定：解析后端先把图片字节 save_image 落盘，得到 "digest/suffix" 形式的映射；
    对无法映射的链接原样保留。
    """
    return _IMG_RE.sub(lambda m: f"{m.group(1)}/media/{m.group(2)}{m.group(3)}", md)


def save_and_rewrite(md: str, images: dict[str, bytes]) -> str:
    """MinerU 场景：{zip 内相对路径: 字节} 全部落盘，md 里的对应引用重写为 /media 路径。"""
    mapping: dict[str, str] = {}
    for rel, data in images.items():
        suffix = os.path.splitext(rel)[1] or ".png"
        web = save_image(data, suffix)
        mapping[rel] = web
        mapping[os.path.basename(rel)] = web

    def _sub(m):
        target = m.group(2)
        for key, web in mapping.items():
            if target.endswith(key) or target == key:
                return f"{m.group(1)}{web}{m.group(3)}"
        return m.group(0)  # 无对应图片字节：保留原引用

    return _IMG_RE.sub(_sub, md)


def extract_image_refs(md: str) -> list[str]:
    """md 里的全部图片 web 路径（视觉描述的输入清单）。"""
    return [m.group(2) for m in _IMG_RE.finditer(md) if m.group(2).startswith("/media/")]


def image_abs_path(web_path: str) -> str | None:
    """web 路径（/media/<digest>/image.png）→ 磁盘绝对路径；越界路径返回 None。"""
    root = os.path.abspath(media_root())
    rel = web_path.removeprefix("/media/").replace("/", os.sep)
    abs_path = os.path.abspath(os.path.join(root, rel))
    if not abs_path.startswith(root):
        return None
    return abs_path if os.path.exists(abs_path) else None

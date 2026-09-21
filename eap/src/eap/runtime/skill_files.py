"""技能附件落盘（M34，L5 工程部分）：scripts/ 与 assets/ 文件的存储与服务。

- 存储：EAP_MEDIA_DIR/skills/<name>/（沿用媒体目录惯例，与库同生命周期、纳入备份范围）
- 安全面：路径校验复用 skill_pkg.validate_asset_path（拒绝绝对路径/`..`、扩展白名单、
  大小/数量上限），落盘与读取前 resolve + is_relative_to 锁定技能目录内
  （与 M28 bundles.install_bundle、M16 media.save_image 同法，防越界）
- 语义：技能附件整体替换——重导入按新包重建目录；清单（SkillRecord.assets）是
  唯一事实源，磁盘上多出的文件不参与列表/下载/打包
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import skill_pkg

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")  # 与 SkillRecord.name 列约束同法（纵深防御）


def skills_root() -> Path:
    """附件根目录：EAP_MEDIA_DIR/skills/（不存在则创建，路径 resolve 后返回）。"""
    from ..config import get_settings

    root = (Path(get_settings().media_dir) / "skills").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def skill_dir(name: str) -> Path:
    """单个技能的附件目录；技能名不合法或解析后越界拒绝（EAP-8104）。"""
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(f"EAP-8104 非法技能名: {name!r}")
    root = skills_root()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("技能附件路径越界，已拒绝")
    return target


def save_assets(name: str, files: list[tuple[str, bytes]]) -> None:
    """附件落盘（整体替换语义）：先清空技能目录，再逐文件写入。

    写入前重跑一次与签名校验同源的安全面（纵深防御：调用方已验签，这里不信任调用方）。
    """
    normalized = [(skill_pkg.validate_asset_path(path), content) for path, content in files]
    skill_pkg.build_asset_manifest(normalized)  # 大小/数量/总量上限（与导入校验同一闸门）
    target = skill_dir(name)
    if target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for rel, content in normalized:
        dest = (target / rel).resolve()
        if not dest.is_relative_to(target):
            raise ValueError(f"EAP-8104 附件落盘路径越界: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


def remove_assets(name: str) -> None:
    """移除技能的全部附件目录（重导入为无附件包时的清场）。"""
    target = skill_dir(name)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def load_asset(name: str, rel_path: str) -> bytes | None:
    """按相对路径取回附件；路径不合法抛 ValueError（EAP-8104），文件不存在返回 None。"""
    normalized = skill_pkg.validate_asset_path(rel_path)
    target = skill_dir(name)
    dest = (target / normalized).resolve()
    if not dest.is_relative_to(target):
        raise ValueError(f"EAP-8104 附件读取路径越界: {rel_path}")
    if not dest.is_file():
        return None
    return dest.read_bytes()


def load_all(name: str) -> list[tuple[str, bytes]]:
    """读取技能目录下全部附件（打包导出用），按路径排序保证确定性。

    打包端随后会用 build_asset_manifest 与库内清单比对（盘上被篡改/混入非法文件
    时响亮失败，不静默跳过）。__pycache__ 等衍生目录不参与（与 M28 pack_bundle 同法）。
    """
    target = skill_dir(name)
    if not target.is_dir():
        return []
    files: list[tuple[str, bytes]] = []
    for path in sorted(target.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files.append((path.relative_to(target).as_posix(), path.read_bytes()))
    return files

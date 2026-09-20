"""Extension Bundle 运行时（docs/unfinished v0.7 / 四十五、四十八节）。

.eapext bundle = zip（manifest.yaml|json + python 代码文件）：
- 安装：安全校验（路径穿越/大小/数量/manifest schema/min_version）→ 解压到
  plugins_dir/<name>/ → 热重载加载 → ExtensionRecord 登记 → 审计
- 升级：同名 version 更高才允许（低版本 409）；卸载：移除目录 + 注销记录
- 打包：pack_bundle(源目录, 输出路径)（scaffold 生成的目录一键成包）
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

MANIFEST_FILENAME = "manifest.json"
MAX_BUNDLE_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 100
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")


def read_bundle(data: bytes) -> tuple[dict, list[tuple[str, bytes]]]:
    """解析 .eapext → (统一 manifest dict, [(相对路径, 字节)])。

    安全面：逐条目拒绝绝对路径/`..`/反斜杠；总量与数量上限；manifest 必须存在。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 包: {e}") from e
    if data and len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle 超过 10MB 上限")
    entries: list[tuple[str, bytes]] = []
    manifest_raw: dict | None = None
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"bundle 内非法路径: {info.filename}")
        content = zf.read(info)
        entries.append((name, content))
        if len(entries) > MAX_ENTRIES:
            raise ValueError("bundle 文件数超过上限 100")
        if name in ("manifest.json", "manifest.yaml", "manifest.yml"):
            manifest_raw = _parse_manifest(content)
    if manifest_raw is None:
        raise ValueError("bundle 缺少 manifest.json / manifest.yaml")
    manifest = _normalize_manifest_file(manifest_raw)
    _validate_bundle_layout(entries, manifest)
    return manifest, entries


def _parse_manifest(content: bytes) -> dict:
    text = content.decode("utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def _normalize_manifest_file(raw: dict) -> dict:
    """manifest 文件支持统一字段；兼容旧 kind 字段归一；min_version 格式即校验。"""
    from .extension_manifest import EXTENSION_TYPES, ExtensionManifest, normalize_eap_plugin

    t = raw.get("type") or raw.get("kind")
    if t not in EXTENSION_TYPES:
        normalized = normalize_eap_plugin(raw)
        raw = {**normalized.model_dump(), "register": raw.get("register") is not None}
    if not raw.get("name"):
        raise ValueError("manifest 缺少 name")
    ExtensionManifest(**{k: v for k, v in raw.items() if k != "register"})  # 格式即校验
    return raw


def _validate_bundle_layout(entries: list[tuple[str, bytes]], manifest: dict) -> None:
    """布局校验：manifest.py 或 __init__.py 必须存在（入口），文件名白名单。"""
    names = {name for name, _ in entries}
    if "manifest.py" not in names and "__init__.py" not in names:
        raise ValueError("bundle 缺少入口（manifest.py 或 __init__.py）")
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if base.endswith(".py") and not re.fullmatch(r"[A-Za-z0-9_]+\.py", base):
            raise ValueError(f"非法的 python 文件名: {base}")


def install_bundle(data: bytes, *, plugins_root: str, actor: str = "system",
                   trace_id: str = "") -> dict:
    """安装 bundle：校验 → 解压 → 热重载 → 登记注册表 → 审计。升级走同名覆盖。"""
    from sqlalchemy import select

    from ..db import SessionLocal
    from .extension_manifest import check_runtime_compatibility, normalize_eap_plugin
    from ..models import ExtensionRecord
    from ..plugins import load_plugins

    manifest, entries = read_bundle(data)
    check_runtime_compatibility(manifest.get("runtime", {}).get("min_version"))
    name = manifest["name"]
    version = manifest["version"]

    with SessionLocal() as db:
        existing = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
        if existing is not None and _ver_key(version) <= _ver_key(existing.version):
            raise ValueError(
                f"EAP-6002 已存在 {name}@{existing.version}，新版本 {version} 不高于已装版本，拒绝降级安装")

    root = Path(plugins_root).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("安装路径越界，已拒绝")
    target.mkdir(parents=True, exist_ok=True)
    for rel, content in entries:
        dest = (target / rel).resolve()
        if not dest.is_relative_to(target):
            raise ValueError(f"安装路径越界: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    load_plugins()  # 热加载（disabled 状态会被 _sync_records 保留）

    with SessionLocal() as db:
        record = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
        state = record.state if record else "registered"
        audit_record("extension.install", actor=actor, target=f"{name}@{version}",
                     detail={"state": state, "entries": len(entries)}, trace_id=trace_id)
    return {"name": name, "version": version, "state": state, "entries": len(entries)}


def uninstall_bundle(name: str, *, plugins_root: str, actor: str = "system",
                     trace_id: str = "") -> dict:
    """卸载扩展：移除插件目录 + 注册表记录（审计）。"""
    import shutil

    from sqlalchemy import select

    from ..db import SessionLocal
    from ..models import ExtensionRecord

    root = Path(plugins_root).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or name.startswith("."):
        raise ValueError("卸载路径越界，已拒绝")
    with SessionLocal() as db:
        record = db.scalar(select(ExtensionRecord).where(ExtensionRecord.name == name))
        existed = record is not None
        version = record.version if record else ""
        if existed:
            db.delete(record)
            db.commit()
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    from ..plugins import reload_plugins

    reload_plugins()  # 清注册副作用：工具/组件从各注册表移除
    audit_record("extension.uninstall", actor=actor, target=f"{name}@{version}",
                 detail={"existed": existed}, trace_id=trace_id)
    return {"name": name, "uninstalled": existed or target.is_dir()}


def pack_bundle(source_dir: str, out_path: str) -> str:
    """目录 → .eapext zip（scaffold 生成的扩展一键打包）。"""
    src = Path(source_dir).resolve()
    if not src.is_dir():
        raise ValueError(f"源目录不存在: {src}")
    has_manifest = any((src / f).exists() for f in ("manifest.json", "manifest.yaml", "manifest.py"))
    if not has_manifest:
        raise ValueError("源目录缺少 manifest（manifest.json/yaml/py）")
    out = Path(out_path)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(src).as_posix())
    return str(out)


def _ver_key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split(".")[:3])


def audit_record(action: str, *, actor: str, target: str, detail: dict | None = None,
                 trace_id: str = "") -> None:
    from ..observability.audit import record

    record(action, actor=actor, target=target, detail=detail or {}, trace_id=trace_id)

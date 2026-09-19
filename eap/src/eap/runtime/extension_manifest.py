"""统一扩展 Manifest（docs/unfinished v0.7-⑨ / 三十六、四十六节）。

所有扩展（agent/tool/rag/workflow_node/connector/ui/model_provider/package）共用
一份 Manifest 契约：声明即权限边界，version 即升级/回滚单位，runtime.min_version
声明兼容的平台版本。旧 EAP_PLUGIN dict 由 normalize_eap_plugin 归一化兼容。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# 扩展类型（docs/unfinished 三十六节）
EXTENSION_TYPES = (
    "agent", "tool", "rag", "workflow_node",
    "connector", "ui", "model_provider", "package",
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ExtensionManifest(BaseModel):
    """统一扩展 Manifest：type 区分扩展种类，其余字段全部类型共享。"""

    apiVersion: str = "eap.io/v1"
    type: str  # EXTENSION_TYPES 之一
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str = ""
    description: str = ""
    runtime: dict = Field(default_factory=dict)  # {"min_version": "0.7.0"}
    permissions: list[str] = Field(default_factory=list)
    dependencies: dict = Field(default_factory=dict)  # {"model": "...", "tools": [...]}
    config_schema: dict = Field(default_factory=dict)  # 声明式配置 Schema
    entry: dict = Field(default_factory=dict)  # 声明式入口提示（module/class）

    @field_validator("type")
    @classmethod
    def check_type(cls, v: str) -> str:
        if v not in EXTENSION_TYPES:
            raise ValueError(f"未知扩展类型 {v!r}（允许: {sorted(EXTENSION_TYPES)}）")
        return v

    @field_validator("runtime")
    @classmethod
    def check_runtime(cls, v: dict) -> dict:
        min_v = v.get("min_version")
        if min_v is not None and not _SEMVER_RE.fullmatch(str(min_v)):
            raise ValueError(f"runtime.min_version 非法: {min_v!r}")
        return v

    def min_version(self) -> str | None:
        min_v = (self.runtime or {}).get("min_version")
        return str(min_v) if min_v else None


def normalize_eap_plugin(manifest: dict) -> ExtensionManifest:
    """旧 EAP_PLUGIN dict → 统一 Manifest（向后兼容，缺 version 补 0.0.0）。"""
    kind = str(manifest.get("kind", "package"))
    ext_type = kind if kind in EXTENSION_TYPES else "package"
    version = str(manifest.get("version") or "0.0.0")
    return ExtensionManifest(
        type=ext_type,
        name=str(manifest.get("name") or "unnamed"),
        version=version,
        title=str(manifest.get("title") or manifest.get("name") or ""),
        description=str(manifest.get("description") or ""),
        permissions=[str(p) for p in manifest.get("permissions", [])],
    )


def check_runtime_compatibility(min_version: str | None) -> None:
    """运行时兼容检查：扩展要求的平台最低版本 > 当前版本 → 拒绝加载。"""
    if not min_version:
        return
    from .. import __version__

    def _parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in str(v).split(".")[:3])

    if _parts(min_version) > _parts(__version__):
        raise ValueError(
            f"扩展要求平台 ≥ {min_version}，当前 {__version__}，请升级 EAP 或降低扩展要求")


def valid_extension_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name))

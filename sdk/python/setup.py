"""setuptools 构建入口：eap-sdk 版本单一事实源 = 平台包 eap 的 __version__（M50-C）。

本目录任何文件不写死版本号（pyproject.toml 声明 dynamic = ["version"]），
构建期按三级顺序推导，杜绝双处手改漂移：

  1. 仓库内平台源码 `eap/src/eap/__init__.py` 的 `__version__`
     —— 树内构建（uv build / CI）的常规路径；
  2. 项目根 `PKG-INFO` —— 从 sdist 异地重建 wheel 时平台源码不在场，
     用 sdist 打包时已固化的元数据版本（setuptools sdist 自带 PKG-INFO）；
  3. 环境变量 `EAP_SDK_VERSION` —— 脱离仓库且无 sdist 的特殊场景显式注入。

三者皆缺 → RuntimeError 快速失败（优于产出错误版本）。
运行期版本单一事实源的另一半在 src/eap_sdk/__init__.py：动态读 eap.__version__。

（选型说明：曾尝试 hatchling 自定义 version source——hatchling 仅对
builder/metadata-hook/build-hook 自动加载 hatch_build.py，version source
不支持本地脚本插件，且版本解析先于 build hook，故用 setuptools 经典
setup.py 动态版本方案，行为等价于 setuptools-scm 的 PKG-INFO 回退。）
"""

from __future__ import annotations

import os
import pathlib
import re

from setuptools import setup

ROOT = pathlib.Path(__file__).resolve().parent
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_PKGINFO_RE = re.compile(r"^Version:\s*(\S+)", re.M)


def _derive_version() -> tuple[str, str]:
    platform_init = ROOT.parent.parent / "eap" / "src" / "eap" / "__init__.py"
    try:
        m = _VERSION_RE.search(platform_init.read_text(encoding="utf-8"))
        if m:
            return m.group(1), f"platform source {platform_init}"
    except OSError:
        pass

    pkginfo = ROOT / "PKG-INFO"
    if pkginfo.is_file():
        m = _PKGINFO_RE.search(pkginfo.read_text(encoding="utf-8"))
        if m:
            return m.group(1), "PKG-INFO (sdist rebuild)"

    env = os.environ.get("EAP_SDK_VERSION")
    if env:
        return env, "env EAP_SDK_VERSION"

    raise RuntimeError(
        "eap-sdk: 无法从平台推导版本——三个来源均缺失："
        f"① 平台源码 {platform_init}；② sdist PKG-INFO；③ 环境变量 EAP_SDK_VERSION。"
        "请在仓库检出内构建，或显式注入版本。"
    )


_version, _origin = _derive_version()
print(f"[eap-sdk] version {_version} (from {_origin})")

setup(version=_version)

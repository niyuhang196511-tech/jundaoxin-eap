#!/usr/bin/env python3
"""导出 OpenAPI 契约（M48-B 开发者体验四件套）：create_app().openapi() → docs/openapi.json。

用法（与 CI backend job 一致；路径相对本脚本解析，与 CWD 无关）：
    cd eap && uv run python ../scripts/export_openapi.py
    # 或仓库根：uv run --project eap python scripts/export_openapi.py

产物 docs/openapi.json 提交入库；CI 每次跑完测试后重新导出并
`git diff --exit-code docs/openapi.json`——接口/Schema 漂移未同步文档即红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "openapi.json"

# 直接以源码树导入 eap（无依赖已安装项目时兜底；已安装则 import 路径优先命中）
_SRC = ROOT / "eap" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> None:
    from eap.main import create_app

    app = create_app()
    schema = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    paths = schema.get("paths", {})
    endpoints = sum(len(ops) for ops in paths.values())  # path × method = 一个 endpoint
    print(f"openapi.json 已导出：{OUT}")
    print(f"paths: {len(paths)}, endpoints: {endpoints}")


if __name__ == "__main__":
    main()

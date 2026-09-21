"""沙箱演示脚本（M33 任务组 P2）：读 stdin 单行 JSON → 回显并附加沙箱环境信息。

经 eap/runtime/sandbox.py 的 SandboxRunner 子进程执行：
- cwd = 一次性临时目录（沙箱根，软隔离约定边界）；
- 环境变量已裁剪（仅 PATH/TEMP/TMP/SYSTEMROOT/LANG 白名单）；
- 协议：单行 JSON 从 stdin 进、stdout 出。

启动示例（独立体验沙箱协议，在 eap/ 目录）：
    echo '{"hello":"world"}' | uv run python examples/sandbox/echo.py
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw}
    print(json.dumps({
        "echo": payload,
        "pwd": os.getcwd(),  # 沙箱根（一次性临时目录）
        "env_keys": sorted(os.environ),  # 环境裁剪后剩余的白名单变量
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

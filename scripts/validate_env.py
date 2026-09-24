#!/usr/bin/env python3
"""M49-A .env 校验工具：在部署前离线校验任意 .env 文件能否被平台安全接受。

校验内容（三层）：
1. 类型/格式：以 init 参数实例化 eap.config.Settings（仅 init 源，屏蔽宿主机
   环境变量与 CWD .env 干扰），pydantic ValidationError 逐条报告；
2. 不安全配置：复用 eap.__main__.insecure_config_problems（与启动 fail-fast
   EAP_STRICT_CONFIG=1 同一份判定，避免逻辑漂移）；
3. 附加约束：密钥字段配置为空串（None 检查覆盖不到的形态）、
   EAP_SECRET_KEY_PREVIOUS 逗号分隔各项非空、EAP_DB_URL scheme 仅 sqlite/postgresql。

用法：
    uv run --no-project python scripts/validate_env.py <env文件路径> [--json]
    # 或（项目 venv 内）：cd eap && uv run python ../scripts/validate_env.py ../deploy/.env.example.prod
    # 裸解释器缺依赖时脚本自动改用 eap/.venv 解释器重跑（--no-project 场景透明兜底）

退出码：0 = 无问题；78 = 存在问题（EX_CONFIG，对齐 __main__.py fail-fast 约定）；
        2 = 用法错误 / 文件不存在 / 依赖缺失。
--json 输出 {"file", "ok", "problems": [{env, reason, line}], "ignored_keys"}。

已知局限：解析器对齐 python-dotenv 的常见语义（注释/引号/export/行内注释），
但不支持跨行引号值；双引号内仅解码 \\n \\r \\t \\\\ \\" 常用转义。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import get_origin

ROOT = Path(__file__).resolve().parent.parent

EX_OK = 0
EX_USAGE = 2  # 用法/文件不存在（sysexits EX_USAGE）
EX_CONFIG = 78  # 配置存在问题（sysexits EX_CONFIG，与 __main__.py fail-fast 对齐）

_NO_REEXEC_ENV = "EAP_VALIDATE_ENV_NO_REEXEC"
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_FROM_MSG_RE = re.compile(r"^(EAP_[A-Z0-9_]+)")
# 双引号值转义（单引号值按 python-dotenv 语义保持字面量）
_DQ_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}
# 复杂类型字段（list/dict/set）的 env 值须为 JSON——与 pydantic-settings EnvSettingsSource 一致
_COMPLEX_ORIGINS = (list, dict, set, frozenset)
# 密钥字段：None 形态由 insecure_config_problems 覆盖，"配置为空串"形态在此兜底
_SECRET_FIELDS = ("dev_api_key", "session_secret", "skill_signing_key", "secret_key")


def _bootstrap() -> None:
    """确保 eap 包与第三方依赖可导入。

    `uv run --no-project python` 用裸解释器（无项目依赖）——此时自动定位
    eap/.venv 的项目解释器并以子进程重跑本脚本（退出码透传），保证任务书
    约定的 CLI 形态开箱可用；找不到 venv 则给出可操作指引并按用法错误退出。
    """
    src = ROOT / "eap" / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import pydantic_settings  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get(_NO_REEXEC_ENV) == "1":
        sys.stderr.write(
            "错误：项目 venv 解释器仍无法导入 pydantic-settings。\n"
            "请先执行：cd eap && uv sync\n"
        )
        raise SystemExit(EX_USAGE)
    candidates = (
        ROOT / "eap" / ".venv" / "Scripts" / "python.exe",  # Windows
        ROOT / "eap" / ".venv" / "bin" / "python",  # POSIX
    )
    for py in candidates:
        if py.is_file():
            env = {**os.environ, _NO_REEXEC_ENV: "1", "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run([str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env)
            raise SystemExit(proc.returncode)
    sys.stderr.write(
        "错误：当前解释器缺少 pydantic-settings 且未找到 eap/.venv。\n"
        "请先执行：cd eap && uv sync，或改用：cd eap && uv run python ../scripts/validate_env.py <文件>\n"
    )
    raise SystemExit(EX_USAGE)


_bootstrap()

from eap.__main__ import insecure_config_problems  # noqa: E402
from eap.config import Settings  # noqa: E402
from pydantic import ValidationError  # noqa: E402


class _InitOnlySettings(Settings):
    """只接受 init 参数的 Settings：屏蔽宿主机环境变量与 CWD .env，保证校验结果只反映目标文件。"""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (init_settings,)


def _problem(env: str | None, reason: str, line: int | None = None) -> dict:
    return {"env": env, "reason": reason, "line": line}


def _parse_value(rest: str, key: str) -> tuple[str, str | None]:
    """解析 = 右侧的值；返回 (值, 错误信息)。语义对齐 python-dotenv。"""
    lstripped = rest.lstrip()
    if lstripped[:1] in ('"', "'"):
        quote = lstripped[0]
        buf: list[str] = []
        i = 1
        closed = False
        while i < len(lstripped):
            ch = lstripped[i]
            if ch == "\\" and quote == '"' and i + 1 < len(lstripped):
                nxt = lstripped[i + 1]
                buf.append(_DQ_ESCAPES.get(nxt, "\\" + nxt))
                i += 2
                continue
            if ch == quote:
                closed = True
                break
            buf.append(ch)
            i += 1
        if not closed:
            return "", f"{key}: 引号未闭合（以 {quote} 开头缺少配对）"
        tail = lstripped[i + 1:].strip()
        if tail and not tail.startswith("#"):
            return "", f"{key}: 闭合引号后存在多余内容 {tail!r}"
        return "".join(buf), None
    # 未加引号：空白+# 起为行内注释（裸 # 属值的一部分，与 python-dotenv 一致），两端去空白
    value = re.split(r"\s#", rest, maxsplit=1)[0]
    return value.strip(), None


def parse_env_text(text: str) -> tuple[dict[str, str], list[dict]]:
    """解析 .env 文本 → (键值映射, 行级语法问题列表)。重复键后者覆盖前者（dotenv 语义）。"""
    values: dict[str, str] = {}
    problems: list[dict] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            problems.append(_problem(None, f"无法解析为 KEY=VALUE（将被平台静默忽略，疑似笔误）：{line!r}", lineno))
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            problems.append(_problem(None, f"非法变量名：{key!r}", lineno))
            continue
        value, err = _parse_value(rest, key)
        if err:
            problems.append(_problem(key, err, lineno))
            continue
        values[key] = value
    return values, problems


def _prepare_init_values(values: dict[str, str]) -> tuple[dict, list[dict], list[str]]:
    """EAP_ 前缀键 → Settings 字段名（init 参数用字段名而非环境变量名）；返回 (init值, 问题, 被忽略键)。"""
    fields = Settings.model_fields
    init: dict = {}
    problems: list[dict] = []
    ignored: list[str] = []
    for key, value in values.items():
        upper = key.upper()
        if not upper.startswith("EAP_"):
            continue  # 非 EAP_ 前缀（POSTGRES_*/NEXT_PUBLIC_* 等）由 compose/前端消费，不属 Settings
        name = upper[len("EAP_"):].lower()
        field = fields.get(name)
        if field is None:
            ignored.append(key)  # extra=ignore 语义：未知 EAP_ 键被平台忽略，此处仅提示不判错
            continue
        if get_origin(field.annotation) in _COMPLEX_ORIGINS:
            try:
                init[name] = json.loads(value)
            except ValueError:
                problems.append(_problem(
                    key, "复杂类型字段须为 JSON 格式（与运行时 env 解析一致，逗号分隔会导致启动失败）"))
            continue
        init[name] = value
    return init, problems, ignored


def _extra_problems(s: Settings) -> list[dict]:
    """附加约束：密钥空串 / SECRET_KEY_PREVIOUS 空项 / DB_URL scheme。"""
    problems: list[dict] = []
    for name in _SECRET_FIELDS:
        v = getattr(s, name, None)
        if isinstance(v, str) and not v.strip():
            problems.append(_problem(
                f"EAP_{name.upper()}", "已配置但为空值——请提供有效值，或整行移除/注释让其走未配置判定"))
    prev = s.secret_key_previous
    if prev is not None:
        items = prev.split(",")
        if any(not it.strip() for it in items):
            problems.append(_problem(
                "EAP_SECRET_KEY_PREVIOUS", f"逗号分隔旧密钥列表存在空项（当前 {len(items)} 项，格式应为 key1,key2）"))
    scheme = re.split(r":/?", s.db_url, maxsplit=1)[0].lower()
    if scheme != "sqlite" and not scheme.startswith("postgresql"):
        problems.append(_problem(
            "EAP_DB_URL", f"scheme 仅支持 sqlite / postgresql（含 postgresql+驱动 形式），当前为 {scheme or '(空)'!r}"))
    return problems


def validate_env_file(path: Path) -> tuple[list[dict], list[str]]:
    """校验单个 .env 文件 → (问题列表, 被忽略的未知 EAP_ 键)。纯函数，可被测试直接调用。"""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [_problem(None, "文件不是有效 UTF-8 编码，无法按 .env 解析")], []
    except OSError as e:
        return [_problem(None, f"无法读取文件：{e}")], []

    values, problems = parse_env_text(text)
    init_values, prep_problems, ignored = _prepare_init_values(values)
    problems += prep_problems

    settings: Settings | None = None
    try:
        settings = _InitOnlySettings(**init_values)
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            problems.append(_problem(f"EAP_{loc.upper()}", f"{err['msg']}（{err['type']}）"))
    if settings is not None:
        # 与启动 fail-fast 同一份判定（eap/__main__.py），消息本身以 EAP_XXX 开头
        for msg in insecure_config_problems(settings):
            m = _ENV_FROM_MSG_RE.match(msg)
            if m and msg[m.end():].startswith(" "):
                problems.append(_problem(m.group(1), msg[m.end():].strip()))  # env 已单列，去消息内重复前缀
            else:
                problems.append(_problem(m.group(1) if m else None, msg))
        problems += _extra_problems(settings)
    return problems, ignored


def _fmt_problem(pb: dict) -> str:
    if pb.get("line") and pb.get("env"):
        return f"第 {pb['line']} 行 {pb['env']}: {pb['reason']}"
    if pb.get("line"):
        return f"第 {pb['line']} 行: {pb['reason']}"
    if pb.get("env"):
        return f"{pb['env']}: {pb['reason']}"
    return pb["reason"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="validate_env",
        description="校验 .env：类型/不安全默认值（与启动 fail-fast 同源）/附加密钥约束",
    )
    p.add_argument("env_file", help="待校验的 .env 文件路径")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（problems 数组，机器可读）")
    args = p.parse_args(argv)

    path = Path(args.env_file)
    if not path.is_file():
        print(f"错误：文件不存在：{path}", file=sys.stderr)
        return EX_USAGE

    problems, ignored = validate_env_file(path)

    if args.json:
        print(json.dumps(
            {"file": path.as_posix(), "ok": not problems, "problems": problems, "ignored_keys": ignored},
            ensure_ascii=False, indent=2))
    else:
        print(f"校验文件：{path}")
        for pb in problems:
            print(f"  - {_fmt_problem(pb)}")
        if ignored:
            print(f"提示：以下 EAP_ 前缀键非 Settings 字段，按 extra=ignore 被平台忽略：{', '.join(ignored)}")
        if problems:
            print(f"结果：发现 {len(problems)} 个问题（退出码 {EX_CONFIG} = EX_CONFIG）")
        else:
            print("结果：校验通过，未发现问题")
    return EX_CONFIG if problems else EX_OK


if __name__ == "__main__":
    sys.exit(main())

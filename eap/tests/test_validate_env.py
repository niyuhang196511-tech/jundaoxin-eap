"""M49-A .env 校验工具测试：scripts/validate_env.py 的退出码、输出与解析语义。

约定（与任务书对齐）：
- 退出码 0 = 无问题；78 = 存在问题（EX_CONFIG，对齐启动 fail-fast）；2 = 用法/文件不存在。
- 脚本以 init 参数实例化 Settings（仅 init 源）——测试同时验证宿主机环境变量不污染结果。
- CLI 断言走 subprocess（sys.executable）；解析器纯函数走 importlib 按路径加载
  （范式同 test_security_rotation.py 的 reencrypt 脚本加载）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate_env.py"

# 合法生产形态：全部不安全默认值已更换、密钥齐备、PG scheme、轮换旧密钥列表合法
GOOD_PROD = """\
EAP_HOST=0.0.0.0
EAP_DB_URL=postgresql+psycopg://eap:pgpass@db:5432/eap
EAP_DEV_API_KEY=prod-api-key-0123456789
EAP_SESSION_SECRET=prod-session-secret-0123456789
EAP_SECRET_KEY=prod-master-key-0123456789
EAP_SKILL_SIGNING_KEY=ed25519-seed-hex-0123456789abcdef
EAP_CORS_ORIGINS=https://console.example.com
EAP_SECRET_KEY_PREVIOUS=old-key-1,old-key-2
"""


def _write(tmp_path: Path, text: str, name: str = ".env") -> Path:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def _run(env_file: Path, *extra: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """子进程跑脚本：强制 UTF-8 IO，其余环境继承（脚本本身对宿主 env 免疫，见 test_host_env_not_polluting）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(env_file), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
    )


def _load_module():
    """scripts/ 非包——按文件路径加载（范式同 test_security_rotation.py:54-68）。"""
    spec = importlib.util.spec_from_file_location("validate_env", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_good_prod_env_exits_zero(tmp_path):
    """合法生产形态：0 问题、退出码 0；--json 结构 ok=true。"""
    f = _write(tmp_path, GOOD_PROD)
    r = _run(f, "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is True
    assert body["problems"] == []


def test_host_env_not_polluting(tmp_path):
    """init 源隔离：宿主 env 注入 EAP_SECRET_KEY 也不能替缺失项“补票”。"""
    f = _write(tmp_path, GOOD_PROD.replace("EAP_SECRET_KEY=prod-master-key-0123456789\n", ""))
    r = _run(f, extra_env={"EAP_SECRET_KEY": "injected-from-host"})
    assert r.returncode == 78, r.stdout
    assert "EAP_SECRET_KEY" in r.stdout


def test_type_error_reported(tmp_path):
    """类型错误（EAP_PORT 非整数）：78，逐条打印 字段: 原因。"""
    f = _write(tmp_path, GOOD_PROD + "EAP_PORT=not-a-number\n")
    r = _run(f)
    assert r.returncode == 78, r.stdout
    assert "EAP_PORT" in r.stdout
    assert "integer" in r.stdout


def test_dev_defaults_flagged(tmp_path):
    """开发默认密钥/缺失密钥：命中 insecure_config_problems（与启动 fail-fast 同源判定）。"""
    f = _write(tmp_path, """\
EAP_DEV_API_KEY=dev-key-1
EAP_SESSION_SECRET=dev-session-secret-change-me
EAP_CORS_ORIGINS=*
""")
    r = _run(f)
    assert r.returncode == 78, r.stdout
    assert "EAP_DEV_API_KEY" in r.stdout
    assert "EAP_SESSION_SECRET" in r.stdout
    assert "仍为开发默认值" in r.stdout
    assert "EAP_CORS_ORIGINS" in r.stdout
    # 未配置的两把密钥也应被点名
    assert "EAP_SECRET_KEY" in r.stdout
    assert "EAP_SKILL_SIGNING_KEY" in r.stdout


def test_empty_secret_and_previous_items(tmp_path):
    """空串密钥（None 检查覆盖不到的形态）与 SECRET_KEY_PREVIOUS 空项都要报。"""
    # 生产模板占位形态：值后跟行内注释 → 解析为空串
    f = _write(tmp_path, GOOD_PROD + "EAP_SESSION_SECRET=        # [*] 待填\n")
    r = _run(f)
    assert r.returncode == 78, r.stdout
    assert "EAP_SESSION_SECRET" in r.stdout
    assert "空" in r.stdout

    f2 = _write(tmp_path, GOOD_PROD + "EAP_SECRET_KEY_PREVIOUS=old1,,old2\n")
    r2 = _run(f2)
    assert r2.returncode == 78, r2.stdout
    assert "EAP_SECRET_KEY_PREVIOUS" in r2.stdout


def test_bad_db_scheme(tmp_path):
    """DB_URL scheme 仅 sqlite/postgresql：mysql 拒绝。"""
    f = _write(tmp_path, GOOD_PROD.replace(
        "postgresql+psycopg://eap:pgpass@db:5432/eap", "mysql://u:p@db:3306/eap"))
    r = _run(f)
    assert r.returncode == 78, r.stdout
    assert "EAP_DB_URL" in r.stdout
    # sqlite 形态合法
    f2 = _write(tmp_path, GOOD_PROD.replace(
        "postgresql+psycopg://eap:pgpass@db:5432/eap", "sqlite:///./eap.db"), name=".env.sqlite")
    assert _run(f2).returncode == 0, r.stdout


def test_missing_file_exits_2(tmp_path):
    """文件不存在：用法类错误，退出码 2。"""
    r = _run(tmp_path / "no-such.env")
    assert r.returncode == 2
    assert "不存在" in r.stderr


def test_json_output_structure_and_ignored_keys(tmp_path):
    """--json：problems 数组元素含 env/reason/line；未知 EAP_ 键进 ignored_keys 不判错。"""
    f = _write(tmp_path, GOOD_PROD + "EAP_VERSION=latest\nEAP_NOT_A_FIELD=1\n")
    r = _run(f, "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is True
    assert set(body["ignored_keys"]) == {"EAP_VERSION", "EAP_NOT_A_FIELD"}

    bad = _write(tmp_path, "EAP_DEV_API_KEY=dev-key-1\n", name=".bad.env")
    r2 = _run(bad, "--json")
    assert r2.returncode == 78
    body2 = json.loads(r2.stdout)
    assert body2["ok"] is False
    assert isinstance(body2["problems"], list) and body2["problems"]
    for pb in body2["problems"]:
        assert set(pb.keys()) == {"env", "reason", "line"}


def test_parser_semantics(tmp_path):
    """解析器纯函数：注释/export/引号/行内注释/裸#/空值/重复键，语义对齐 python-dotenv。"""
    mod = _load_module()
    f = _write(tmp_path, """\
# 整行注释
export EXPORTED=ev
QUOTED="a # b"
SINGLE='raw # val'
PLAIN=value  # 行内注释
HASHMID=abc#def
EMPTY=
DUP=1
DUP=2
""")
    values, problems = mod.parse_env_text(f.read_text(encoding="utf-8"))
    assert problems == []
    assert values["EXPORTED"] == "ev"
    assert values["QUOTED"] == "a # b"
    assert values["SINGLE"] == "raw # val"
    assert values["PLAIN"] == "value"
    assert values["HASHMID"] == "abc#def"
    assert values["EMPTY"] == ""
    assert values["DUP"] == "2"  # 后者覆盖前者

    # 语法坏行：无 = 与引号未闭合 → 行级问题（计入退出码 78）
    _, bad_problems = mod.parse_env_text("NOT_KV_LINE\nBROKEN=\"unterminated\n")
    assert len(bad_problems) == 2
    assert bad_problems[0]["line"] == 1
    assert bad_problems[1]["line"] == 2 and bad_problems[1]["env"] == "BROKEN"


def test_complex_field_requires_json(tmp_path):
    """list 字段与运行时 env 解析一致须为 JSON：逗号分隔会导致启动失败，校验器必须报。"""
    mod = _load_module()
    init_values, problems, _ignored = mod._prepare_init_values({"EAP_AGENT_MODULES": "a,b"})
    assert init_values == {}
    assert len(problems) == 1 and problems[0]["env"] == "EAP_AGENT_MODULES"
    init_ok, problems_ok, _ = mod._prepare_init_values({"EAP_AGENT_MODULES": '["a", "b"]'})
    assert problems_ok == [] and init_ok == {"agent_modules": ["a", "b"]}

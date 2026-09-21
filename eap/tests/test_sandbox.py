"""Sandbox 测试（M33 任务组 P2）：SandboxRunner 子进程受限执行 + tool-sandbox 策略联动。

全部离线确定：子进程用 sys.executable 跑临时脚本文件（无 shell 拼接）；
resource/setrlimit 为 POSIX 专属，相关用例 skipif win32（开发机 Windows，
超时/FS 约定/env 裁剪用例跨平台可跑）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from .conftest import AUTH

_EAP_ROOT = Path(__file__).resolve().parents[1]
ECHO_SCRIPT = _EAP_ROOT / "examples" / "sandbox" / "echo.py"


def _write_script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "s.py"
    script.write_text(body, encoding="utf-8")
    return str(script)


def _metric(key: str) -> float:
    """从 Prometheus 渲染文本取计数器当前值（无该序列时 0）。"""
    from eap.observability.metrics import render

    for line in render().splitlines():
        if line.startswith(key + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _drop_policy(name: str) -> None:
    """直删策略行（测试隔离：不污染同会话其他租户用例）。"""
    from eap.db import SessionLocal
    from eap.models import PolicyRecord
    from sqlalchemy import delete

    with SessionLocal() as db:
        db.execute(delete(PolicyRecord).where(PolicyRecord.name == name))
        db.commit()


def _make_sandbox_policy(client, name: str, tools: list[str], *, mode: str = "enforce",
                         tenant: int = 42) -> dict:
    resp = client.post("/api/v1/policies", headers=AUTH,
                       json={"name": name, "tenant_id": tenant, "kind": "tool-sandbox",
                             "config": {"mode": mode, "tools": tools}, "priority": 10})
    assert resp.status_code == 200, resp.text
    return resp.json()


class _FakeHub:
    """最小假模型（行为对齐 modelhub MockProvider）：首轮发起 tools[0] 调用，
    拿到工具结果后输出最终回答——离线确定的 run_loop 驱动。"""

    def __init__(self):
        self.called: list[str] = []

    async def complete(self, db, msgs, **kwargs):
        tools = kwargs.get("tools")
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        if tools and not has_tool_result:
            fn = tools[0]["function"]
            self.called.append(fn["name"])
            result = SimpleNamespace(
                content="", tokens_in=0, tokens_out=0, data=None,
                tool_calls=[SimpleNamespace(id="call-1", name=fn["name"], arguments="{}")])
            return SimpleNamespace(result=result, record=SimpleNamespace(name="fake-mock"))
        result = SimpleNamespace(content="done", tokens_in=0, tokens_out=0,
                                 data=None, tool_calls=[])
        return SimpleNamespace(result=result, record=SimpleNamespace(name="fake-mock"))


# ---------- SandboxRunner ----------

def test_echo_roundtrip(tmp_path):
    """echo 脚本往返：args JSON stdin 进 → 结果 JSON stdout 出（线程池异步入口）。"""
    from eap.runtime.sandbox import SandboxRunner

    payload = {"hello": "沙箱", "n": 3}
    result = asyncio.run(SandboxRunner().run_async(
        str(ECHO_SCRIPT), json.dumps(payload, ensure_ascii=False), timeout_s=30))
    assert result["ok"], result
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    out = json.loads(result["stdout"])
    assert out["echo"] == payload
    # 沙箱根约定：脚本 cwd = 一次性临时目录（而非平台进程 cwd）
    assert Path(os.path.normcase(out["pwd"])) == Path(os.path.normcase(result["sandbox_dir"]))
    assert Path(os.path.normcase(out["pwd"])) != Path(os.path.normcase(str(Path.cwd())))


def test_timeout_kills_script(tmp_path):
    """超时脚本被杀：timed_out=true、ok=false，且耗时受限（非等满 sleep）。"""
    from eap.runtime.sandbox import SandboxRunner

    script = _write_script(tmp_path, "import time\nprint('start', flush=True)\ntime.sleep(60)\n")
    result = SandboxRunner().run(script, "{}", timeout_s=2)
    assert result["timed_out"] is True
    assert result["ok"] is False
    assert result["duration_ms"] < 20000, result


def test_fs_isolation_convention(tmp_path):
    """FS 隔离（软隔离约定检查）：cwd=一次性沙箱根，相对写入落在沙箱根内，
    平台进程工作目录不被污染；沙箱根执行后销毁（cleanup=True）。"""
    from eap.runtime.sandbox import SandboxRunner

    script = _write_script(tmp_path, (
        "import json, os\n"
        "with open('out.txt', 'w', encoding='utf-8') as f:\n"
        "    f.write('sandbox')\n"
        "print(json.dumps({'pwd': os.getcwd()}))\n"
    ))
    runner = SandboxRunner(cleanup=False)  # 保留沙箱根以断言写入落点
    result: dict = {}
    result2: dict = {}
    try:
        result = runner.run(script, "{}", timeout_s=30)
        assert result["ok"], result
        pwd = json.loads(result["stdout"])["pwd"]
        assert Path(os.path.normcase(pwd)) == Path(os.path.normcase(result["sandbox_dir"]))
        assert (Path(pwd) / "out.txt").exists()  # 相对写入落在沙箱根内
        assert not (Path.cwd() / "out.txt").exists()  # 平台进程 cwd 未被污染
        # 每次执行独立沙箱根
        result2 = SandboxRunner(cleanup=False).run(script, "{}", timeout_s=30)
        assert result2["sandbox_dir"] != result["sandbox_dir"]
    finally:
        for r in (result, result2):
            if r.get("sandbox_dir"):
                shutil.rmtree(r["sandbox_dir"], ignore_errors=True)


def test_env_scrubbed():
    """环境裁剪：宿主注入的敏感变量不进沙箱，白名单变量（PATH）保留。"""
    from eap.runtime.sandbox import SandboxRunner

    os.environ["EAP_SANDBOX_SECRET"] = "s3cret-inject"
    try:
        result = SandboxRunner().run(str(ECHO_SCRIPT), "{}", timeout_s=30)
    finally:
        os.environ.pop("EAP_SANDBOX_SECRET", None)
    assert result["ok"], result
    out = json.loads(result["stdout"])
    assert "EAP_SANDBOX_SECRET" not in out["env_keys"]
    assert "PATH" in out["env_keys"]
    assert "env_scrub" in result["limits_applied"]


@pytest.mark.skipif(sys.platform == "win32", reason="resource/setrlimit 为 POSIX 专属")
def test_mem_limit_posix(tmp_path):
    """POSIX 内存限额：RLIMIT_AS 超限 → 分配失败、非零退出（error，非超时）。"""
    from eap.runtime.sandbox import SandboxRunner

    script = _write_script(tmp_path, "data = bytearray(512 * 1024 * 1024)\nprint('allocated')\n")
    result = SandboxRunner().run(script, "{}", timeout_s=30, mem_mb=64)
    assert result["ok"] is False
    assert result["timed_out"] is False
    assert result["exit_code"] != 0
    assert "rlimit_mem" in result["limits_applied"]


# ---------- script_tool 工厂全链路 ----------

def test_script_tool_full_chain():
    """script_tool 全链路：注册 → find_tool → handler → 沙箱子进程执行回注。"""
    from eap.runtime.tools import build_tools, find_tool, script_tool

    tool = script_tool("sbx_echo", "沙箱回显", {"type": "object"}, str(ECHO_SCRIPT),
                       risk_level="medium", timeout_s=30)
    tools = build_tools(tool)  # 注册表构建（含重名检查）
    found = find_tool(tools, "sbx_echo")
    assert found is not None
    assert found.runtime == "script"
    assert found.script_path == str(ECHO_SCRIPT)
    out = asyncio.run(found.handler(json.dumps({"k": "v"})))
    data = json.loads(out)
    assert data["echo"] == {"k": "v"}
    assert Path(os.path.normcase(data["pwd"])) != Path(os.path.normcase(str(Path.cwd())))


def test_script_tool_failure_feedback(tmp_path):
    """脚本失败（非零退出）回注 {"error": ...}，与进程内工具失败语义一致。"""
    from eap.runtime.tools import script_tool

    script = _write_script(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nraise SystemExit(3)\n")
    tool = script_tool("sbx_fail", "失败脚本", {"type": "object"}, script, timeout_s=30)
    out = asyncio.run(tool.handler("{}"))
    data = json.loads(out)
    assert "error" in data
    assert "boom" in data["error"] or "exit_code=3" in data["error"]


# ---------- tool-sandbox 策略联动 ----------

def test_sandbox_policy_api_validation(client):
    """策略 API：tool-sandbox kind 校验（tools 列表 + mode 枚举）。"""
    resp = client.post("/api/v1/policies", headers=AUTH,
                       json={"name": "sbx-bad-tools", "tenant_id": 42, "kind": "tool-sandbox",
                             "config": {"mode": "enforce"}, "priority": 10})
    assert resp.status_code == 400  # 缺 tools 列表
    resp = client.post("/api/v1/policies", headers=AUTH,
                       json={"name": "sbx-bad-mode", "tenant_id": 42, "kind": "tool-sandbox",
                             "config": {"mode": "block", "tools": ["a"]}, "priority": 10})
    assert resp.status_code == 400  # mode 非法
    data = _make_sandbox_policy(client, "sbx-api-ok-t42", ["t"], mode="audit")
    assert data["kind"] == "tool-sandbox"
    assert data["config"] == {"mode": "audit", "tools": ["t"]}
    _drop_policy("sbx-api-ok-t42")


def test_sandbox_policy_decisions(client):
    """check_tool_sandbox 决策矩阵：enforce+inproc=deny / script=sandbox /
    audit+inproc=audit；清单外 None。"""
    _make_sandbox_policy(client, "sbx-enforce-t42", ["sbx_inproc_tool", "sbx_script_tool"],
                         mode="enforce")
    _make_sandbox_policy(client, "sbx-audit-t42", ["sbx_audit_tool"], mode="audit")
    from eap.db import SessionLocal
    from eap.runtime.policy import check_tool_sandbox, reset_tenant, set_tenant

    token = set_tenant(42)
    try:
        with SessionLocal() as db:
            deny = check_tool_sandbox(db, "sbx_inproc_tool", "inproc")
            assert deny["action"] == "deny"
            assert "被要求沙箱执行但为进程内实现" in deny["reason"]
            sandbox = check_tool_sandbox(db, "sbx_script_tool", "script")
            assert sandbox["action"] == "sandbox"  # 脚本工具必须走沙箱（防绕过）
            audit = check_tool_sandbox(db, "sbx_audit_tool", "inproc")
            assert audit["action"] == "audit"
            assert check_tool_sandbox(db, "unlisted_tool", "inproc") is None
            assert check_tool_sandbox(db, "unlisted_tool", "script") is None
    finally:
        reset_tenant(token)
        _drop_policy("sbx-enforce-t42")
        _drop_policy("sbx-audit-t42")


def test_loop_routes_script_tool_via_sandbox(client):
    """loop 集成（enforce）：清单内脚本工具经沙箱路由执行，进程内 handler 不被调用
    （毒丸 handler 防绕过验证）；指标 +1、审计 status=ok。"""
    _make_sandbox_policy(client, "sbx-loop-t43", ["sbx_loop_echo"], mode="enforce", tenant=43)
    from eap.db import SessionLocal
    from eap.runtime.loop import run_loop
    from eap.runtime.policy import reset_tenant, set_tenant
    from eap.runtime.tools import Tool

    async def _poison(args: str) -> str:  # 被调用即说明沙箱路由失效
        raise AssertionError("沙箱路由未生效：进程内 handler 被调用")

    bypass_tool = Tool(name="sbx_loop_echo", description="防绕过验证",
                       parameters={"type": "object"}, handler=_poison,
                       runtime="script", script_path=str(ECHO_SCRIPT), timeout_s=30)
    hub = _FakeHub()
    token = set_tenant(43)
    try:
        with SessionLocal() as db:
            before = _metric('eap_sandbox_exec_total{result="ok"}')
            rr = asyncio.run(run_loop(hub, db, messages=[{"role": "user", "content": "hi"}],
                                      system="s", tools=[bypass_tool]))
            after = _metric('eap_sandbox_exec_total{result="ok"}')
        assert rr.content == "done", rr.steps  # 脚本正常执行 → 回合正常收束
        assert hub.called == ["sbx_loop_echo"]
        assert after == pytest.approx(before + 1)  # 沙箱执行恰好一次（ok）
        logs = client.get("/api/v1/audit", headers=AUTH,
                          params={"action": "tool.call", "target": "sbx_loop_echo"}).json()
        assert any(l["detail"].get("status") == "ok" for l in logs)
    finally:
        reset_tenant(token)
        _drop_policy("sbx-loop-t43")


def test_loop_enforce_denies_inproc_tool(client):
    """loop 集成（enforce）：清单内 inproc 工具执行前被拒（错误回注模型而非崩溃），
    violation 审计 + 指标落账。"""
    _make_sandbox_policy(client, "sbx-deny-t43", ["sbx_loop_inproc"], mode="enforce", tenant=43)
    from eap.db import SessionLocal
    from eap.runtime.loop import run_loop
    from eap.runtime.policy import reset_tenant, set_tenant
    from eap.runtime.tools import Tool

    async def _never(args: str) -> str:
        raise AssertionError("enforce 拒绝后进程内 handler 不应被调用")

    inproc = Tool(name="sbx_loop_inproc", description="进程内工具",
                  parameters={"type": "object"}, handler=_never, timeout_s=30)
    hub = _FakeHub()
    token = set_tenant(43)
    try:
        with SessionLocal() as db:
            before = _metric('eap_sandbox_violations_total{mode="enforce"}')
            rr = asyncio.run(run_loop(hub, db, messages=[{"role": "user", "content": "hi"}],
                                      system="s", tools=[inproc]))
            after = _metric('eap_sandbox_violations_total{mode="enforce"}')
        assert rr.content == "done"  # 拒绝结果回注模型，循环仍收束
        assert after == pytest.approx(before + 1)  # violation 指标 +1（enforce）
        logs = client.get("/api/v1/audit", headers=AUTH,
                          params={"action": "tool.sandbox.violation",
                                  "target": "sbx_loop_inproc"}).json()
        assert logs, "enforce 拒绝应落 tool.sandbox.violation 审计"
        assert logs[0]["detail"]["mode"] == "enforce"
        calls = client.get("/api/v1/audit", headers=AUTH,
                           params={"action": "tool.call", "target": "sbx_loop_inproc"}).json()
        assert any(c["detail"].get("status") == "error" for c in calls)
    finally:
        reset_tenant(token)
        _drop_policy("sbx-deny-t43")

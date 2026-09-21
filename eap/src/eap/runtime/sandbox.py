"""工具/脚本沙箱（M33 任务组 P2）：外部脚本的子进程受限执行引擎。

隔离语义（诚实标注——软隔离：约定 + 环境裁剪，非容器级）：
- 文件系统：cwd 指向一次性临时目录（沙箱根，每次执行新建、执行后销毁），
  相对路径写入落在沙箱根内随执行销毁；**绝对路径越界写入无法技术阻断**
  （不 mounts 不 chroot，避免引入容器依赖），依赖脚本约定 + 策略审计兜底。
- 环境裁剪：仅透传 PATH/TEMP/TMP/SYSTEMROOT(Windows)/LANG 白名单变量，
  其余（含密钥类注入）全部剔除（另注入 PYTHONIOENCODING/PYTHONUTF8 两个
  协议变量强制子进程 UTF-8，非宿主变量）。
- 资源限制：POSIX 经 preexec_fn 设置 RLIMIT_CPU / RLIMIT_AS；
  **Windows 降级语义：resource/setrlimit 为 POSIX 专属，Windows 仅强制
  超时 + 文件系统隔离 + 环境裁剪**（limits_applied 字段如实反映实际生效项）。
- 超时：先 terminate（POSIX 进程组 / Windows 单进程）宽限数秒，仍存活再 kill。

协议：单行 JSON 从 stdin 进、stdout 出。异常/超时不外抛，一律返回结构化结果
（调用方决定回注语义）。执行在 asyncio.to_thread 线程池承载，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import functools
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from ..observability.metrics import incr

_POSIX = os.name == "posix"

# 环境裁剪白名单（M33）：默认仅透传这些变量，其余剔除
_ENV_ALLOWLIST = ("PATH", "TEMP", "TMP", "SYSTEMROOT", "LANG")

_MAX_OUTPUT_CHARS = 256 * 1024  # stdout/stderr 单边上限（字符数，近似 256KB）
_TRUNCATE_MARK = "\n…[沙箱输出超过 256KB 已截断]"
_KILL_GRACE_S = 3.0  # terminate → kill 的宽限期


def _preexec_limits(cpu_s: int | None, mem_mb: int | None) -> None:
    """POSIX preexec_fn：子进程资源限额（Windows 不调用）。"""
    import resource

    if cpu_s and cpu_s > 0:
        cap = int(cpu_s)
        resource.setrlimit(resource.RLIMIT_CPU, (cap, cap))
    if mem_mb and mem_mb > 0:
        cap = int(mem_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


def _kill_tree(proc: subprocess.Popen, posix: bool) -> None:
    """进程树终止：先 terminate 宽限 _KILL_GRACE_S，仍存活再 kill。

    POSIX 用进程组（start_new_session 建组，os.killpg 覆盖子孙进程）；
    Windows 无进程组语义，proc.terminate/kill 仅覆盖直接子进程（已知降级）。
    """
    def _send(kill: bool) -> None:
        try:
            if posix:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL if kill else signal.SIGTERM)
            else:
                (proc.kill if kill else proc.terminate)()
        except (ProcessLookupError, PermissionError, OSError):
            pass  # 进程已退出/权限不足即视为完成

    _send(kill=False)
    try:
        proc.wait(timeout=_KILL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    _send(kill=True)


class SandboxRunner:
    """子进程沙箱执行器（进程内引擎复用：模块级默认实例经 get_runner() 共享）。

    cleanup=False 保留沙箱根目录（测试断言写入落点用），默认执行后销毁。
    """

    def __init__(self, *, cleanup: bool = True):
        self.cleanup = cleanup

    async def run_async(self, script_path: str, input_json: str, *, timeout_s: float,
                        mem_mb: int | None = None, cpu_s: int | None = None) -> dict:
        """线程池承载的阻塞执行（防阻塞事件循环）。"""
        return await asyncio.to_thread(self.run, script_path, input_json,
                                       timeout_s=timeout_s, mem_mb=mem_mb, cpu_s=cpu_s)

    def run(self, script_path: str, input_json: str, *, timeout_s: float,
            mem_mb: int | None = None, cpu_s: int | None = None) -> dict:
        """阻塞执行脚本（同步入口；事件循环请用 run_async）。

        返回 {ok, exit_code, stdout, stderr, duration_ms, timed_out,
        limits_applied, truncated, sandbox_dir}。异常/超时不外抛。
        """
        t0 = time.monotonic()
        timed_out = False
        stdout = stderr = ""
        exit_code: int | None = None
        sandbox_dir = tempfile.mkdtemp(prefix="eap-sandbox-")

        # 环境裁剪：仅白名单变量透传（值存在才带）
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if os.environ.get(k)}
        # 协议变量（非宿主注入）：强制子进程 stdio/文件系统 UTF-8，
        # 避免中文 Windows 等非 UTF-8 locale 下 JSON 编解码错乱
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # 资源限额：POSIX preexec_fn；Windows 无 rlimit（降级语义见模块 docstring）
        limits_applied = ["timeout", "fs_isolation", "env_scrub"]
        popen_kwargs: dict = {}
        if _POSIX:
            popen_kwargs["start_new_session"] = True  # 建独立进程组，供 killpg 树杀
            if (cpu_s or 0) > 0 or (mem_mb or 0) > 0:
                popen_kwargs["preexec_fn"] = functools.partial(_preexec_limits, cpu_s, mem_mb)
                if (cpu_s or 0) > 0:
                    limits_applied.append("rlimit_cpu")
                if (mem_mb or 0) > 0:
                    limits_applied.append("rlimit_mem")

        try:
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=sandbox_dir, env=env, text=True, encoding="utf-8", errors="replace",
                **popen_kwargs)
        except Exception as e:  # 启动失败同样结构化返回，不上抛
            result = self._result(False, None, "", f"沙箱启动失败: {e}", t0, False,
                                  limits_applied, {"stdout": False, "stderr": False}, sandbox_dir)
            incr("eap_sandbox_exec_total", {"result": "error"})
            self._cleanup(sandbox_dir)
            return result

        try:
            stdout, stderr = proc.communicate(input=input_json, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc, _POSIX)
            try:  # 重试回收（不丢失已缓冲输出）
                stdout, stderr = proc.communicate(input=None, timeout=_KILL_GRACE_S + 2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                stdout, stderr = "", "（沙箱进程终止阶段未退出，输出丢弃）"
        except Exception as e:  # 通信层异常（管道断裂等）
            _kill_tree(proc, _POSIX)
            stderr = f"沙箱通信异常: {e}"

        exit_code = proc.returncode
        truncated = {"stdout": False, "stderr": False}
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + _TRUNCATE_MARK
            truncated["stdout"] = True
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS] + _TRUNCATE_MARK
            truncated["stderr"] = True

        ok = (not timed_out) and exit_code == 0
        result = self._result(ok, exit_code, stdout, stderr, t0, timed_out,
                              limits_applied, truncated, sandbox_dir)
        incr("eap_sandbox_exec_total", {"result": "timeout" if timed_out else ("ok" if ok else "error")})
        self._cleanup(sandbox_dir)
        return result

    @staticmethod
    def _result(ok: bool, exit_code: int | None, stdout: str, stderr: str, t0: float,
                timed_out: bool, limits_applied: list, truncated: dict, sandbox_dir: str) -> dict:
        return {
            "ok": ok,
            "exit_code": exit_code,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "timed_out": timed_out,
            "limits_applied": limits_applied,
            "truncated": truncated,
            "sandbox_dir": sandbox_dir,
        }

    def _cleanup(self, sandbox_dir: str) -> None:
        if self.cleanup:
            shutil.rmtree(sandbox_dir, ignore_errors=True)


_default_runner = SandboxRunner()


def get_runner() -> SandboxRunner:
    """进程内共享引擎：同一 runner 复用（线程池由 asyncio.to_thread 承载）。"""
    return _default_runner

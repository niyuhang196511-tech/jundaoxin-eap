"""M55-A 双进程压测：脚本 smoke 测试 + 独立 worker 注册引导回归守卫。

不在 pytest 里做重压测（CI 时长敏感）：quick 模式子进程跑一遍全阶段
（50 网关请求 / 20 任务 / 幂等双通道，实测 ~21s，timeout 守卫 150s），
断言退出码 0 与 RESULT_JSON 的关键不变量（429 生效且计数无漂移 / 幂等
跨副本命中 / 任务全终态 / 双进程执行归因）。

test_worker_bootstraps_agent_registry 是 M55-A 压测发现的真 bug 的回归守卫：
独立 worker（python -m eap.worker）执行 agent.invoke 前必须 registry.bootstrap
——修复前 bootstrap 仅在 API 进程 lifespan 执行，HA 形态（API
EAP_WORKER_COUNT=0 + 独立 worker，deploy/docker-compose.ha.yml 同款拓扑）下
worker 注册表为空，agent 任务全数 FAILED（"智能体 faq-agent 未注册"）。
无 Redis 即可验证：AsyncioQueueBackend + 启动期 _recover_pending 捞起预置的
PENDING 任务并执行，终态落库后断言 COMPLETED。

Redis 守卫照 tests/test_multireplica._redis_up 模式（compose redis 63790），
仅用于 smoke 用例（回归守卫不依赖 Redis，离线可跑）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from .test_multireplica import _redis_up

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "loadtest_dualproc.py")
_RESULT_MARKER = "RESULT_JSON: "

requires_redis = pytest.mark.skipif(
    not _redis_up(), reason="本地 63790 无 Redis（compose redis 未起/CI 无 service）")


@requires_redis
def test_quick_smoke_exit_0_and_invariants():
    """quick 模式冒烟：退出码 0 + RESULT_JSON 关键不变量 + ~60s 预算护栏。"""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, _SCRIPT, "--quick"], cwd=_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=150)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"quick 压测超 150s 预算（应 ~60s）：{(e.stdout or '')[-3000:]}")
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, (
        f"quick 压测退出码 {proc.returncode}（应 0，耗时 {elapsed:.0f}s）\n"
        f"stdout 尾部:\n{proc.stdout[-4000:]}\nstderr 尾部:\n{proc.stderr[-2000:]}")
    assert elapsed < 120, f"quick 压测耗时 {elapsed:.0f}s，超 ~60s 预算的两倍护栏"
    line = next(l for l in proc.stdout.splitlines() if l.startswith(_RESULT_MARKER))
    r = json.loads(line[len(_RESULT_MARKER):])
    # ① 网关分布式限流：全局额度下 429 生效、共享计数落 Redis 且终态无漂移
    assert r["gateway"]["rejected429"] > 0
    assert r["gateway"]["errors5xx"] == 0 and r["gateway"]["conn_errors"] == 0
    assert r["gateway"]["other_4xx"] == 0
    assert r["gateway"]["ok200"] + r["gateway"]["rejected429"] == r["gateway"]["sent"]
    assert r["gateway"]["conc_probe_seen"] is True
    assert r["gateway"]["conc_key_final"] in (None, "0")
    # ② 幂等跨副本：任务级同 key 命中同一 task_id + HTTP 级跨副本重放
    assert r["task_idempotency"]["distinct_task_ids"] == 1
    assert r["task_idempotency"]["existing_hits"] == r["task_idempotency"]["same_key_submits"] - 1
    assert r["http_idempotency"]["http_replay_ok"] is True
    # ③ 任务全终态 + 双进程执行归因（XPENDING 捕获 ≥2 个执行消费者）
    assert r["tasks"]["completed"] == r["tasks"]["submitted"] and r["tasks"]["failed"] == 0
    assert r["tasks"]["leftover_active"] == []
    assert r["tasks"]["consumers_multi_process"] is True


def test_worker_bootstraps_agent_registry(tmp_path):
    """回归守卫（M55-A 真 bug）：独立 worker 执行 agent.invoke 到 COMPLETED。

    预置一条 PENDING agent.invoke 任务（直插 TaskRecord）→ 起 python -m eap.worker
    子进程 → 启动期 _recover_pending 恢复入队 → registry.bootstrap 后的注册表执行。
    修复前该任务 FAILED（'智能体 faq-agent 未注册'）。
    """
    run = uuid.uuid4().hex[:8]
    task_id = f"worker-reg-{run}"
    db_path = (tmp_path / "wreg.db").as_posix()
    env = os.environ.copy()
    env.update({
        "EAP_DB_URL": f"sqlite:///{db_path}",
        "EAP_SKIP_MIGRATIONS": "1",  # 父子进程并发 Alembic 会 SQLite 锁等待（conftest 同款纪律）
        "EAP_WORKER_COUNT": "1",
    })
    env.pop("EAP_REDIS_URL", None)  # 无 Redis → AsyncioQueueBackend，_recover_pending 恢复预置任务
    setup = subprocess.run(
        [sys.executable, "-c",
         "from eap.db import init_db; init_db()\n"
         "from eap.db import SessionLocal\n"
         "from eap.models import TaskRecord\n"
         "db = SessionLocal()\n"
         f"db.add(TaskRecord(id={task_id!r}, type='agent.invoke', state='PENDING',\n"
         "                  payload={'agent': 'faq-agent', 'input': 'worker 注册引导回归'}))\n"
         "db.commit(); db.close()\n"
         "print('seeded')"],
        env=env, cwd=_ROOT, capture_output=True, text=True, timeout=120)
    assert setup.returncode == 0, f"预置任务失败:\n{setup.stdout[-2000:]}\n{setup.stderr[-2000:]}"

    log_path = tmp_path / "worker.log"
    with open(log_path, "wb") as log:
        worker = subprocess.Popen([sys.executable, "-m", "eap.worker"], env=env, cwd=_ROOT,
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            poll = subprocess.run(
                [sys.executable, "-c",
                 "import sqlite3, sys, time\n"
                 f"con = sqlite3.connect({db_path!r}, timeout=10)\n"
                 "deadline = time.time() + 75\n"
                 "state = None\n"
                 "while time.time() < deadline:\n"
                 f"    row = con.execute('SELECT state FROM tasks WHERE id=?', ({task_id!r},)).fetchone()\n"
                 "    state = row[0] if row else None\n"
                 "    if state in ('COMPLETED', 'FAILED'):\n"
                 "        break\n"
                 "    time.sleep(0.4)\n"
                 "print(state or 'TIMEOUT')"],
                timeout=90, capture_output=True, text=True)
            state = poll.stdout.strip().splitlines()[-1] if poll.stdout.strip() else "NO_OUTPUT"
        finally:
            worker.terminate()
            worker.wait(timeout=15)
    assert state == "COMPLETED", (
        f"独立 worker 应执行 agent.invoke 到 COMPLETED（修复前 FAILED：智能体未注册），"
        f"实际 {state}\nworker 日志:\n{log_path.read_text(errors='replace')[-2000:]}")

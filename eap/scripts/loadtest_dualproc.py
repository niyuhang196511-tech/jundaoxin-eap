#!/usr/bin/env python
"""M55-A：双进程并发压测（两个独立 uvicorn 进程 + 独立 worker 进程）。

账本既有开放项（M51 登记的实测项）：既有验证多为进程内双实例模拟（TestClient
两个 app）或单进程，本脚本以真实多进程负载验证 M32/M49-B 的分布式语义：

① 网关分布式限流：两进程同一 Bearer 凭证并发打满 EAP_GATEWAY_MAX_CONCURRENCY，
   断言 429 全局生效（共享 eap:gw:conc:<cred> 计数）、终态计数归零（无漂移/泄漏）；
② 幂等跨副本：
   - 任务级（M32 body 字段 idempotency_key）：同 key 提交轮换打两进程，命中 existing
     返回同一 task_id（worker 未启动、任务恒 PENDING 在途 → 命中确定性地成立）；
   - HTTP 级（M31 网关 Idempotency-Key 头）：A 执行 → B 同键重放（X-Idempotent-Replay）；
③ 任务租约/抢占：独占 worker 进程执行大文本 kb.ingest，捕获 RUNNING 后强杀该进程，
   存活侧（新 worker 启动时的租约恢复扫描）接管重跑到终态——at-least-once 语义；
④ 延迟/错误率基线：p50/p95/p99 与错误率打印，末尾 RESULT_JSON 一行机读汇总。

架构：
- 2 × API 进程（python -m eap，端口 port_base/port_base+1）：EAP_WORKER_COUNT=0
  （HA 形态：API 不执行任务，deploy/compose 同款）、EAP_GATEWAY_MAX_CONCURRENCY=3、
  EAP_REDIS_URL=redis://…/8（db8 专用，跑前 flushdb，不碰 db5/db6/db9 纪律）；
- 2 × worker 进程（python -m eap.worker）：同一 Redis Streams 消费组跨进程消费，
  XINFO CONSUMERS 的 per-consumer deliveries = 双进程执行分发的直接证据；
- 同一持久 SQLite（$TEMP 下临时库，跑完删除，--keep 保留调试）；
- 提交侧 round-robin 轮换两进程 = 双进程轮询分发；同一 Bearer 凭证（seed dev-key-1）。

用法：
    uv run python scripts/loadtest_dualproc.py            # 全量（200 任务/500 请求+租约阶段）
    uv run python scripts/loadtest_dualproc.py --quick    # 冒烟（20 任务/50 请求，无租约阶段）
退出码：0 成功；1 断言失败；2 环境不可用（Redis 不可达/端口被占）；3 启动失败。

诚实边界：本机 Windows 单机双进程 ≠ 真分布式多机（无网络分区、无跨主机时钟偏差；
SQLite 单写者串行化也弱于 PG 并发写），结论仅代表单机多进程拓扑下的语义正确性。
「不双跑」无执行计数器可观测（处理器无副作用计数），以结构性论证 + 无 FAILED/无滞留
+ 租约恢复重跑覆盖，详见报告。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

EAP_ROOT = Path(__file__).resolve().parent.parent  # eap/（含 alembic.ini 与 src/eap）
DEFAULT_PORT_BASE = 18300
DEFAULT_REDIS_URL = "redis://localhost:63790/8"
API_KEY = "dev-key-1"  # seed.run 种子凭证（EAP_DEV_API_KEY 默认值）
AUTH = {"Authorization": f"Bearer {API_KEY}"}
GATEWAY_CONCURRENCY = 3  # 每凭证在途上限（两进程共享同一全局额度）
WORKER_LEASE_S = 5  # 租约缩短：强杀后存活侧尽快接管（生产默认 300，语义不变）
HEALTH_TIMEOUT_S = 60.0
RESULT_MARKER = "RESULT_JSON: "

# 网关并发计数键（gateway.credential_of 同式：key:<sha256(token)[:16]>）
_CONC_KEY = "eap:gw:conc:key:" + hashlib.sha256(API_KEY.encode()).hexdigest()[:16]


# ---------------- 基础工具 ----------------

def _pct(values: list[float], p: int) -> float:
    """百分位（毫秒）：空列表回 0.0。"""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return round(s[idx], 2)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _redis_client(url: str):
    import redis as redis_sync

    return redis_sync.from_url(url, decode_responses=True,
                               socket_timeout=5.0, socket_connect_timeout=3.0)


def _redis_up(url: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 6379), timeout=2):
            return True
    except OSError:
        return False


def _tail(path: str, lines: int = 40) -> str:
    try:
        with open(path, "rb") as f:
            chunk = f.read()[-8000:]
        return "\n".join(chunk.decode("utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "<无日志>"


def _say(msg: str) -> None:
    print(msg, flush=True)


# ---------------- 子进程管理 ----------------

def _child_env(db_url: str, redis_url: str, tmp: str, **extra) -> dict:
    env = os.environ.copy()
    env.update({
        "EAP_DB_URL": db_url,
        "EAP_REDIS_URL": redis_url,
        "EAP_SKIP_MIGRATIONS": "1",  # 库已由 init 子进程迁到 head（父子并发 upgrade 会锁等待）
        "EAP_WORKER_COUNT": "0",  # HA 形态：API 进程不执行任务（deploy/compose 同款）
        "EAP_MEDIA_DIR": os.path.join(tmp, "media"),
        "EAP_PLUGINS_DIR": os.path.join(tmp, "plugins"),
        "EAP_WORKER_LEASE_SECONDS": str(WORKER_LEASE_S),
        "EAP_GATEWAY_MAX_CONCURRENCY": str(GATEWAY_CONCURRENCY),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _spawn(name: str, args: list[str], env: dict, tmp: str) -> dict:
    log_path = os.path.join(tmp, f"{name}.log")
    fh = open(log_path, "ab")
    proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=str(EAP_ROOT))
    return {"name": name, "proc": proc, "log": log_path, "fh": fh}


def _stop(p: dict, timeout: float = 10.0) -> None:
    if p["proc"].poll() is None:
        p["proc"].terminate()  # Windows：TerminateProcess（硬杀；优雅停机语义由 test_worker 覆盖）
        try:
            p["proc"].wait(timeout)
        except subprocess.TimeoutExpired:
            p["proc"].kill()
            p["proc"].wait(timeout)
    if not p["fh"].closed:
        p["fh"].close()


def _alive(p: dict) -> bool:
    return p["proc"].poll() is None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def _wait_health(client, ports: list[int], procs: dict) -> None:
    """两 API 进程 /health 就绪轮询；子进程先死立即报日志尾部（不傻等）。"""
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    pending = set(ports)
    while pending and time.monotonic() < deadline:
        for port in list(pending):
            try:
                r = await client.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
                if r.status_code == 200:
                    pending.discard(port)
            except Exception:
                pass
        for p in procs.values():
            if not _alive(p):
                raise AssertionError(
                    f"子进程 {p['name']} 提前退出（code={p['proc'].returncode}）\n{_tail(p['log'])}")
        if pending:
            await asyncio.sleep(0.25)
    if pending:
        raise AssertionError(f"/health 就绪超时（{HEALTH_TIMEOUT_S}s）：{sorted(pending)}")


def _wait_log_marker(p: dict, marker: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker in _tail(p["log"], 200):
            return True
        if not _alive(p):
            return False
        time.sleep(0.2)
    return False


# ---------------- 压测阶段 ----------------

async def phase_gateway(clients: list, r_count: int, redis_url: str) -> dict:
    """① 网关分布式限流：同一凭证并发打两进程，全局额度 EAP_GATEWAY_MAX_CONCURRENCY。

    探针线程旁路采样共享计数键：键出现过 = 槽位记账真实落 Redis（而非静默降级
    进程内计数）；终态值 ∈ {None,'0'} = 大量 INCR+DECR 回滚后无漂移/无泄漏。
    注：INCR→评估→DECR 回滚窗口内计数可瞬时超限（实现如此），故 max 仅观测不断言。
    """
    probe = {"seen": False, "max": None}
    stop = threading.Event()

    def _probe() -> None:
        try:
            r = _redis_client(redis_url)
        except Exception:
            return
        try:
            while not stop.is_set():
                v = r.get(_CONC_KEY)
                if v is not None:
                    probe["seen"] = True
                    iv = int(v)
                    probe["max"] = iv if probe["max"] is None else max(probe["max"], iv)
                stop.wait(0.002)
        except Exception:
            pass  # 探针失败不影响主流程（最终以终态键值断言兜底）
        finally:
            try:
                r.close()
            except Exception:
                pass

    t = threading.Thread(target=_probe, daemon=True)
    t.start()

    async def one(i: int):
        cli = clients[i % 2]
        t0 = time.perf_counter()
        try:
            resp = await cli.get("/api/v1/tasks", params={"limit": 1})
            return resp.status_code, (time.perf_counter() - t0) * 1000, i % 2
        except Exception:
            return -1, (time.perf_counter() - t0) * 1000, i % 2

    t_start = time.monotonic()
    results = await asyncio.gather(*(one(i) for i in range(r_count)))
    wall_s = time.monotonic() - t_start
    stop.set()
    t.join(timeout=5)

    lat = {"ok": [], "r429": []}
    status: dict[int, int] = {}
    by_side_ok = [0, 0]
    by_side_429 = [0, 0]
    for st, ms, side in results:
        status[st] = status.get(st, 0) + 1
        if st == 200:
            lat["ok"].append(ms)
            by_side_ok[side] += 1
        elif st == 429:
            lat["r429"].append(ms)
            by_side_429[side] += 1

    r = _redis_client(redis_url)
    try:
        final = r.get(_CONC_KEY)
    finally:
        r.close()

    errors_conn = status.get(-1, 0)
    errors_5xx = sum(v for k, v in status.items() if 500 <= k <= 599)
    other_4xx = sum(v for k, v in status.items() if 400 <= k <= 499 and k != 429)
    ok200 = status.get(200, 0)
    r429 = status.get(429, 0)
    return {
        "sent": r_count, "ok200": ok200, "rejected429": r429,
        "errors5xx": errors_5xx, "conn_errors": errors_conn, "other_4xx": other_4xx,
        "error_rate": round((errors_conn + errors_5xx + other_4xx) / r_count, 4),
        "p50_ms_admitted": _pct(lat["ok"], 50), "p95_ms_admitted": _pct(lat["ok"], 95),
        "p99_ms_admitted": _pct(lat["ok"], 99),
        "p50_ms_rejected": _pct(lat["r429"], 50), "p95_ms_rejected": _pct(lat["r429"], 95),
        "wall_s": round(wall_s, 2),
        "ok200_by_side": by_side_ok, "rejected429_by_side": by_side_429,
        "status_detail": {str(k): v for k, v in sorted(status.items())},
        "conc_key_final": final, "conc_probe_seen": probe["seen"], "conc_probe_max": probe["max"],
        "unknown_statuses": {str(k): v for k, v in status.items() if k not in (200, 429, -1)
                             and not 400 <= k <= 599},
    }


async def phase_task_idempotency(clients: list, k: int, run_id: str) -> dict:
    """② 任务级幂等（M32 body 字段）：同 key 轮换打两进程，worker 未启动任务恒 PENDING
    在途 → 命中确定成立；断言 K 次提交同一 task_id、恰一次 existing=False。"""
    key = f"lt-{run_id}-idem"
    body = {"type": "agent.invoke",
            "payload": {"agent": "faq-agent", "input": f"幂等跨副本验证 {run_id}"},
            "idempotency_key": key}
    ids: list[str] = []
    existing: list[bool] = []
    for i in range(k):
        resp = await clients[i % 2].post("/api/v1/tasks", json=body)
        _check(resp.status_code == 200, f"任务幂等提交 #{i} 非 200：{resp.status_code} {resp.text[:200]}")
        data = resp.json()
        ids.append(data["task_id"])
        existing.append(data["existing"])
    _check(not existing[0], f"首次提交应 existing=False（新键）：{existing[0]}")
    _check(all(existing[1:]), f"后续 {k - 1} 次同 key 提交应全部命中 existing=True：{existing}")
    _check(len(set(ids)) == 1, f"同 key 提交应返回同一 task_id：{set(ids)}")
    return {"same_key_submits": k, "existing_hits": sum(existing), "distinct_task_ids": len(set(ids)),
            "task_id": ids[0], "task_id_reused_across_both_processes": True}


async def phase_http_idempotency(clients: list, run_id: str) -> dict:
    """② HTTP 级幂等（M31 网关 Idempotency-Key 头）：A 执行 → B 同键重放同响应体。"""
    hdr = {"Idempotency-Key": f"hdr-{run_id}"}
    body = {"type": "agent.invoke",
            "payload": {"agent": "faq-agent", "input": f"HTTP 幂等重放验证 {run_id}"},
            "idempotency_key": f"hdr-task-{run_id}"}
    ra = await clients[0].post("/api/v1/tasks", json=body, headers=hdr)
    _check(ra.status_code == 200, f"HTTP 幂等首次提交非 200：{ra.status_code}")
    _check("x-idempotent-replay" not in ra.headers, "首次提交不应带重放标记")
    rb = await clients[1].post("/api/v1/tasks", json=body, headers=hdr)
    _check(rb.status_code == 200, f"HTTP 幂等跨副本重放非 200：{rb.status_code}")
    _check(rb.headers.get("x-idempotent-replay") == "true",
           f"B 进程应重放（X-Idempotent-Replay: true），实际头：{dict(rb.headers)}")
    _check(rb.content == ra.content, "重放响应体应与首次响应体逐字节一致")
    return {"http_replay_ok": True, "first_task_id": ra.json()["task_id"],
            "replayed_body_identical": True}


async def _count_state(clients: list, sem: asyncio.Semaphore, state: str) -> int:
    total, offset = 0, 0
    while True:
        async with sem:
            resp = None
            for attempt in range(3):  # SQLite 写者竞争下读偶发 locked→500：同请求重试
                resp = await clients[0].get("/api/v1/tasks",
                                            params={"state": state, "limit": 200, "offset": offset})
                if resp.status_code == 200:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        _check(resp.status_code == 200, f"列任务({state}) 重试后仍非 200：{resp.status_code}")
        batch = resp.json()
        total += len(batch)
        if len(batch) < 200:
            return total
        offset += 200


async def phase_task_load(clients: list, n: int, run_id: str, procs: dict, env: dict,
                          tmp: str, deadline_s: float) -> dict:
    """③④ 任务负载：起两个独立 worker 进程（同消费组跨进程消费），N 个任务经两进程
    轮换提交（semaphore 2 < 全局并发额度 3，杜绝限流噪声）；轮询至全部终态；
    XINFO CONSUMERS 的 per-consumer deliveries = 双进程执行分发的直接证据。"""
    for tag in ("worker1", "worker2"):
        procs[tag] = _spawn(tag, [sys.executable, "-m", "eap.worker"], env, tmp)
        # 串行起（前一进程就绪再起下一个）：worker 启动也跑 create_all+seed，
        # 并发首启会撞 seed/registry 的唯一约束（首跑实测 api1/api2 同发即撞）
        _check(_wait_log_marker(procs[tag], "就绪", 30),
               f"{tag} 未在 30s 内就绪\n{_tail(procs[tag]['log'])}")

    sem = asyncio.Semaphore(2)
    lat: list[float] = []
    by_side = [0, 0]

    # 执行归属探针（Redis 6.2 无 XINFO CONSUMERS.deliveries，改用 XPENDING 归因）：
    # 任务执行期间其 stream 消息处于 pending（未 ack），XPENDING 条目的 consumer 字段
    # 即「实际执行该消息的进程消费者」——API 进程只有 reclaim 循环（reclaim 只重投
    # 不执行，且 min_idle=60s 期间消息远不到），故捕获到的名字都是独立 worker 进程。
    executors: set[str] = set()
    probe_stop = asyncio.Event()

    async def _pending_probe() -> None:
        import redis as redis_sync

        r = redis_sync.from_url(env["EAP_REDIS_URL"], decode_responses=True,
                                socket_timeout=5.0, socket_connect_timeout=3.0)
        try:
            while not probe_stop.is_set():
                for stream in ("eap:tasks", "eap:tasks:hi"):
                    try:
                        for entry in r.xpending_range(stream, "eap-workers", "-", "+", 100):
                            executors.add(entry["consumer"])
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(probe_stop.wait(), 0.02)
                except asyncio.TimeoutError:
                    pass
        finally:
            r.close()

    probe_task = asyncio.create_task(_pending_probe())

    async def submit(i: int) -> str:
        async with sem:
            body = {"type": "agent.invoke",
                    "payload": {"agent": "faq-agent", "input": f"负载测试 {run_id} #{i}"},
                    "idempotency_key": f"lt-{run_id}-t{i}"}
            t0 = time.perf_counter()
            data = None
            last = ""
            for attempt in range(3):  # SQLite 写者竞争偶发 locked→500：同 key 重试去重安全
                resp = await clients[i % 2].post("/api/v1/tasks", json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                last = f"{resp.status_code}: {resp.text[:200]}"
                await asyncio.sleep(0.5 * (attempt + 1))
            lat.append((time.perf_counter() - t0) * 1000)
            by_side[i % 2] += 1
            _check(data is not None, f"任务 #{i} 提交重试 3 次仍失败：{last}")
            _check(not data["existing"], f"任务 #{i} 唯一键不应命中 existing：{data}")
            return data["task_id"]

    t_start = time.monotonic()
    task_ids = await asyncio.gather(*(submit(i) for i in range(n)))
    submit_wall = time.monotonic() - t_start
    # B1/B2 各遗留 1 个 PENDING 任务，也由本轮 worker 完成执行
    expected_total = n + 2
    _check(len(set(task_ids)) == n, "任务 id 应全唯一")

    states = ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT")
    while True:
        counts = {s: await _count_state(clients, sem, s) for s in states}
        if sum(counts.values()) == 0:
            break
        if time.monotonic() - t_start > deadline_s:
            break
        await asyncio.sleep(0.5)

    completed = await _count_state(clients, sem, "COMPLETED")
    failed = await _count_state(clients, sem, "FAILED")
    leftover = [s for s in states if counts[s] > 0]
    e2e_s = time.monotonic() - t_start
    probe_stop.set()
    await probe_task

    import redis as redis_sync

    r = redis_sync.from_url(env["EAP_REDIS_URL"], decode_responses=True,
                            socket_timeout=5.0, socket_connect_timeout=3.0)
    try:
        consumers = r.xinfo_consumers("eap:tasks", "eap-workers")
    finally:
        r.close()
    consumer_names = sorted(c["name"] for c in consumers)
    return {
        "submitted": expected_total, "completed": completed, "failed": failed,
        "leftover_active": leftover,
        "submit_wall_s": round(submit_wall, 2), "e2e_s": round(e2e_s, 2),
        "submit_p50_ms": _pct(lat, 50), "submit_p95_ms": _pct(lat, 95), "submit_p99_ms": _pct(lat, 99),
        "submit_by_side": by_side,
        "consumers_executing": sorted(executors),
        "consumers_on_group": consumer_names,
        "consumers_multi_process": len(executors) >= 2,
    }


async def phase_lease_takeover(clients: list, run_id: str, procs: dict, env: dict,
                               tmp: str, sem: asyncio.Semaphore, completed_before: int) -> dict:
    """③ 租约恢复：停 worker2（空闲）→ 仅 worker1 独占消费；提交大文本 kb.ingest，
    捕获 RUNNING 后强杀 worker1（任务遗留 RUNNING+租约）→ 等租约过期起 worker3，
    其启动期租约恢复扫描重置 PENDING 接管重跑。断言全部终态 COMPLETED（无滞留）。
    处理器非幂等（每次执行建一个文档）：文档数 > 任务数即 at-least-once 重跑的直接痕迹。"""
    _stop(procs.pop("worker2"))  # 空闲进程干净退出，保证 RUNNING 必属 worker1

    kb = f"loadtest-{run_id}"
    resp = await clients[0].post("/api/v1/kb", json={"name": kb, "title": "M55-A 租约压测"})
    _check(resp.status_code == 200, f"建知识库非 200：{resp.status_code} {resp.text[:200]}")

    para = "租约恢复压测段落：worker 进程崩溃后任务由存活副本经租约扫描接管重跑（at-least-once）。"
    big_text = para * 800  # ≈135KB，单次摄入秒级 → RUNNING 窗口可捕获；过大 INSERT 会撞 SQLite 写锁
    t_submit = time.monotonic()

    async def submit_big(i: int) -> str:
        async with sem:
            body = {"type": "kb.ingest",
                    "payload": {"kb": kb, "title": f"lease-{i}", "text": big_text},
                    "idempotency_key": f"lt-{run_id}-lease{i}"}
            last = ""
            for attempt in range(4):  # 大 payload INSERT 与 worker 写入竞争 SQLite 写锁：同 key 重试去重安全
                resp = await clients[i % 2].post("/api/v1/tasks", json=body)
                if resp.status_code == 200:
                    return resp.json()["task_id"]  # 重试命中 existing=True 时返回同一 task_id
                last = f"{resp.status_code}: {resp.text[:200]}"
                await asyncio.sleep(1.0 * (attempt + 1))
            raise AssertionError(f"大任务 #{i} 重试 4 次仍失败：{last}")

    big_ids = await asyncio.gather(*(submit_big(i) for i in range(6)))
    _check(len(set(big_ids)) == 6, "大任务 id 应全唯一")

    observed = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        observed = await _count_state(clients, sem, "RUNNING")
        if observed > 0:
            break
        await asyncio.sleep(0.005)

    orphan_id = None
    t_kill = None
    orphan_confirmed = False
    if observed > 0:
        # 记下 RUNNING 任务 id：杀掉执行方后它若仍是 RUNNING 即「孤儿」实锤
        async with sem:
            rlist = await clients[0].get("/api/v1/tasks", params={"state": "RUNNING", "limit": 10})
        _check(rlist.status_code == 200, f"取 RUNNING 列表失败：{rlist.status_code}")
        running_rows = rlist.json()
        if running_rows:
            orphan_id = running_rows[0]["task_id"]
        _stop(procs.pop("worker1"))  # 硬杀：执行中任务遗留 RUNNING + 租约
        t_kill = time.monotonic()
        if orphan_id is not None:
            resp = await clients[0].get(f"/api/v1/tasks/{orphan_id}")
            if resp.status_code == 200 and resp.json()["state"] == "RUNNING":
                orphan_confirmed = True  # 执行方已死而任务仍 RUNNING：只能靠租约恢复重跑
        await asyncio.sleep(WORKER_LEASE_S + 2)  # 等租约过期（快于任何恢复扫描周期）
        procs["worker3"] = _spawn("worker3", [sys.executable, "-m", "eap.worker"], env, tmp)
        _check(_wait_log_marker(procs["worker3"], "就绪", 30),
               f"worker3 未就绪\n{_tail(procs['worker3']['log'])}")
        t_worker3_ready = time.monotonic()

    # 孤儿任务定点追踪：worker3 起来后该任务应在租约恢复+重跑下到 COMPLETED
    orphan_state = None
    if orphan_id is not None:
        orphan_deadline = time.monotonic() + 120
        while time.monotonic() < orphan_deadline:
            resp = await clients[0].get(f"/api/v1/tasks/{orphan_id}")
            _check(resp.status_code == 200, f"孤儿任务查询失败：{resp.status_code}")
            orphan_state = resp.json()["state"]
            if orphan_state in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            await asyncio.sleep(0.5)
    recovery_s = (round(time.monotonic() - t_worker3_ready, 2)
                  if t_kill and orphan_state == "COMPLETED" else None)

    recover_deadline = time.monotonic() + 90
    while True:
        counts = {s: await _count_state(clients, sem, s)
                  for s in ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT")}
        if sum(counts.values()) == 0 or time.monotonic() > recover_deadline:
            break
        await asyncio.sleep(0.5)
    completed = await _count_state(clients, sem, "COMPLETED")
    failed = await _count_state(clients, sem, "FAILED")
    leftover = [s for s in ("PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_INPUT") if counts[s] > 0]

    resp = await clients[0].get(f"/api/v1/kb/{kb}/documents")
    docs = len(resp.json()) if resp.status_code == 200 else -1
    return {
        "tasks": 6, "observed_running_at_kill": observed,
        "killed_executor": "worker1" if observed > 0 else None,
        "orphan_task_id": orphan_id, "orphan_confirmed": orphan_confirmed,
        "orphan_final_state": orphan_state,
        "recovery_s": recovery_s,
        "takeover_s": round(time.monotonic() - t_kill, 2) if t_kill else None,
        "completed_before": completed_before,
        "completed_cumulative": completed, "failed_cumulative": failed,
        "leftover_active": leftover, "docs_in_kb": docs,
        "rerun_evidence": (docs > 6) if docs >= 0 else None,
        "big_text_kb": round(len(big_text.encode()) / 1024, 1),
        "submit_to_kill_s": round((t_kill - t_submit), 2) if t_kill else None,
    }


# ---------------- 报告 ----------------

def _report(args, summary: dict) -> None:
    gw, idem, http, tasks, lease = (summary.get("gateway"), summary.get("task_idempotency"),
                                    summary.get("http_idempotency"), summary.get("tasks"),
                                    summary.get("lease"))
    _say("")
    _say("=" * 68)
    _say("M55-A 双进程并发压测报告（真实多进程：2×uvicorn API + 2×独立 worker）")
    _say("=" * 68)
    _say(f"[架构] API 进程 2 × 127.0.0.1:{args.port_base}/{args.port_base + 1} "
         f"(EAP_WORKER_COUNT=0, EAP_GATEWAY_MAX_CONCURRENCY={GATEWAY_CONCURRENCY})")
    _say(f"       worker 进程 2 × python -m eap.worker (workers=2/进程, lease={WORKER_LEASE_S}s)")
    _say(f"       DB={summary.get('db_url')}  Redis={args.redis_url}（跑前 flushdb）")
    if gw:
        _say(f"[① 网关限流] 发 {gw['sent']}：200×{gw['ok200']}（进程分布 {gw['ok200_by_side']}），"
             f"429×{gw['rejected429']}（进程分布 {gw['rejected429_by_side']}），"
             f"5xx×{gw['errors5xx']}，连接错×{gw['conn_errors']}，其他4xx×{gw['other_4xx']}，"
             f"错误率={gw['error_rate']:.2%}，耗时 {gw['wall_s']}s")
        _say(f"       延迟(ms) 放行 p50/p95/p99 = {gw['p50_ms_admitted']}/{gw['p95_ms_admitted']}"
             f"/{gw['p99_ms_admitted']}；429 p50/p95 = {gw['p50_ms_rejected']}/{gw['p95_ms_rejected']}")
        _say(f"       共享计数键 eap:gw:conc:* 压测中出现={gw['conc_probe_seen']}，"
             f"峰值(观测)={gw['conc_probe_max']}，终态={gw['conc_key_final']!r}（None/'0'=无漂移）")
    if idem:
        _say(f"[② 任务级幂等] 同 key 提交×{idem['same_key_submits']}（轮换两进程）："
             f"existing 命中×{idem['existing_hits']}，distinct task_id={idem['distinct_task_ids']}")
    if http:
        _say(f"[② HTTP级幂等] 跨副本重放={'通过' if http['http_replay_ok'] else '未通过'}"
             f"（B 进程 X-Idempotent-Replay=true，响应体逐字节一致）")
    if tasks:
        _say(f"[③ 任务负载] 提交 {tasks['submitted']}（轮换两进程 {tasks['submit_by_side']}，"
             f"提交 {tasks['submit_wall_s']}s）→ COMPLETED×{tasks['completed']}，FAILED×{tasks['failed']}，"
             f"滞留={tasks['leftover_active']}")
        _say(f"       提交延迟(ms) p50/p95/p99 = {tasks['submit_p50_ms']}/{tasks['submit_p95_ms']}"
             f"/{tasks['submit_p99_ms']}；端到端 {tasks['e2e_s']}s")
        _say(f"       执行分发（XPENDING 归因，执行中消息的 consumer）：{tasks['consumers_executing']}"
             f" → 双进程执行={'是' if tasks['consumers_multi_process'] else '否'}；"
             f"消费组成员={tasks['consumers_on_group']}")
    if lease:
        _say(f"[③ 租约恢复] 大文本 kb.ingest×{lease['tasks']}（{lease['big_text_kb']}KB/条）→ "
             f"捕获 RUNNING×{lease['observed_running_at_kill']} 后强杀 {lease['killed_executor']}；"
             f"孤儿实锤（执行方死后仍 RUNNING）={'是' if lease['orphan_confirmed'] else '否'}；"
             f"孤儿重跑耗时 {lease['recovery_s']}s（自 worker3 就绪），终态 {lease['orphan_final_state']}；"
             f"终态 COMPLETED(累计)×{lease['completed_cumulative']}，FAILED×{lease['failed_cumulative']}，"
             f"滞留={lease['leftover_active']}")
        _say(f"       知识库文档×{lease['docs_in_kb']}（任务×{lease['tasks']}）→ "
             f"强杀时首跑未落文档（重跑补齐 1 文档/任务）；at-least-once 语义已由孤儿重跑实证"
             if lease['orphan_confirmed'] else
             f"       知识库文档×{lease['docs_in_kb']}（任务×{lease['tasks']}）→ "
             f"at-least-once 重跑痕迹=未捕获（强杀未打断在途执行）")
    _say("=" * 68)


# ---------------- 主流程 ----------------

async def run_loadtest(args) -> int:
    import httpx

    run_id = uuid.uuid4().hex[:8]
    tmp = tempfile.mkdtemp(prefix="eap-loadtest-")
    os.makedirs(os.path.join(tmp, "media"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "plugins"), exist_ok=True)
    db_url = f"sqlite:///{(Path(tmp) / 'loadtest.db').as_posix()}"
    env = _child_env(db_url, args.redis_url, tmp)
    procs: dict[str, dict] = {}
    clients: list = []
    sem: asyncio.Semaphore | None = None
    summary: dict = {"run_id": run_id, "db_url": db_url, "quick": args.quick}
    try:
        # 0) 环境预检 + 专用库清场（db8 不碰 db5/db6/db9 纪律）
        for port in (args.port_base, args.port_base + 1):
            if not _port_free(port):
                _say(f"[启动失败] 端口 {port} 已被占用（疑似残留进程），退出码 2")
                return 2
        r = _redis_client(args.redis_url)
        try:
            r.flushdb()
        finally:
            r.close()
        _say(f"[预检] Redis={args.redis_url} 已 flushdb；临时库={db_url}")

        # 1) 建库迁移动作只跑一次（独立 init 子进程，随后子进程 EAP_SKIP_MIGRATIONS=1）
        init_env = {k: v for k, v in env.items() if k != "EAP_SKIP_MIGRATIONS"}
        init = subprocess.run([sys.executable, "-c", "from eap.db import init_db; init_db()"],
                              env=init_env, cwd=str(EAP_ROOT), capture_output=True,
                              text=True, timeout=180)
        if init.returncode != 0:
            _say(f"[启动失败] init_db 子进程失败：\n{init.stdout[-2000:]}\n{init.stderr[-2000:]}")
            return 3

        # 2) 起 2 个 uvicorn API 进程（串行首启：registry.bootstrap/seed 并发首启会撞
        #    agents.name 唯一约束——首跑实测；先就绪再起下一个 = 滚动启动语义）
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        timeout = httpx.Timeout(30.0)
        for tag, port in (("api1", args.port_base), ("api2", args.port_base + 1)):
            procs[tag] = _spawn(tag, [sys.executable, "-m", "eap"],
                                _child_env(db_url, args.redis_url, tmp, EAP_PORT=port), tmp)
            client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=AUTH,
                                       limits=limits, timeout=timeout)
            await _wait_health(client, [port], procs)
            clients.append(client)
        probe = await clients[0].get("/api/v1/tasks", params={"limit": 1})
        _check(probe.status_code == 200, f"带凭证鉴权探测非 200（seed 未就绪？）：{probe.status_code}")
        _say(f"[启动] api1/api2 就绪（:{args.port_base}/:{args.port_base + 1}，日志在 {tmp}）")

        # ① 网关分布式限流（同一凭证并发打满两进程共享的全局额度）
        _say(f"[阶段①] 网关限流：{args.requests} 并发请求 × 同一凭证 × 两进程轮换 …")
        summary["gateway"] = await phase_gateway(clients, args.requests, args.redis_url)
        gw = summary["gateway"]
        _check(gw["conn_errors"] == 0, f"存在连接错误：{gw}")
        _check(gw["errors5xx"] == 0, f"存在 5xx：{gw['status_detail']}")
        _check(gw["other_4xx"] == 0, f"存在预期外 4xx：{gw['status_detail']}")
        _check(gw["ok200"] + gw["rejected429"] == gw["sent"],
               f"200+429 应等于总数：{gw['status_detail']}")
        _check(gw["rejected429"] > 0, "未触发任何 429：全局并发限制未生效")
        _check(gw["conc_probe_seen"], f"共享计数键 {_CONC_KEY} 未在压测中出现：槽位记账未落 Redis")
        _check(gw["conc_key_final"] in (None, "0"),
               f"压测后共享计数应归零（无漂移/泄漏），实际 {gw['conc_key_final']!r}")

        # ② 幂等跨副本（worker 未启动 → 任务恒在途，命中确定性成立）
        _say(f"[阶段②] 幂等跨副本：同 key×{args.idem_submits} 轮换两进程 + HTTP Idempotency-Key 重放 …")
        summary["task_idempotency"] = await phase_task_idempotency(clients, args.idem_submits, run_id)
        summary["http_idempotency"] = await phase_http_idempotency(clients, run_id)

        # ③④ 任务负载 + 双进程执行分发
        _say(f"[阶段③] 任务负载：起 2 个 worker 进程，提交 {args.tasks} 任务轮换两进程 …")
        sem = asyncio.Semaphore(2)
        worker_env = _child_env(db_url, args.redis_url, tmp, EAP_WORKER_COUNT=2)
        summary["tasks"] = await phase_task_load(clients, args.tasks, run_id, procs, worker_env,
                                                 tmp, deadline_s=60.0 if args.quick else 150.0)
        _check(summary["tasks"]["failed"] == 0,
               f"任务不应 FAILED：completed={summary['tasks']['completed']}")
        _check(summary["tasks"]["completed"] == summary["tasks"]["submitted"],
               f"应全部 COMPLETED：{summary['tasks']}")
        _check(not summary["tasks"]["leftover_active"],
               f"存在滞留在途任务：{summary['tasks']['leftover_active']}")
        _check(summary["tasks"]["consumers_multi_process"],
               f"执行分发应跨 ≥2 个 worker 进程：{summary['tasks']['consumers_executing']}")

        # ③+ 租约恢复（强杀执行中 worker → 存活侧接管重跑）
        if not args.skip_lease_probe:
            _say("[阶段③+] 租约恢复：独占 worker 执行大文本 kb.ingest，捕获 RUNNING 后强杀 …")
            completed_before = summary["tasks"]["completed"]
            summary["lease"] = await phase_lease_takeover(clients, run_id, procs, worker_env, tmp,
                                                          sem, completed_before)
            lease = summary["lease"]
            _check(not lease["leftover_active"], f"租约阶段存在滞留任务（恢复失效？）：{lease}")
            _check(lease["failed_cumulative"] == 0, f"租约阶段不应 FAILED：{lease}")
            _check(lease["completed_cumulative"] == completed_before + lease["tasks"],
                   f"6 个大任务应全部终态 COMPLETED（含被强杀者接管重跑）：{lease}")
            if lease["orphan_confirmed"]:
                _check(lease["orphan_final_state"] == "COMPLETED",
                       f"孤儿任务（执行方被强杀后仍 RUNNING）应经租约恢复重跑到 COMPLETED：{lease}")
                _check(lease["recovery_s"] is not None and lease["recovery_s"] < 60,
                       f"孤儿任务应在 worker3 就绪后 60s 内重跑完成：{lease}")

        summary["ports"] = [args.port_base, args.port_base + 1]
        _report(args, summary)
        _say(RESULT_MARKER + json.dumps(summary, ensure_ascii=False))
        return 0
    except AssertionError as e:
        _say(f"\n[断言失败] {e}")
        for p in procs.values():
            _say(f"---- {p['name']} 日志尾部 ----\n{_tail(p['log'], 25)}")
        _say(RESULT_MARKER + json.dumps({"failed": True, "error": str(e)[:500],
                                         "partial": summary}, ensure_ascii=False))
        return 1
    except Exception as e:  # noqa: BLE001 —— 任何意外都要求清理并带日志退出
        _say(f"\n[异常] {type(e).__name__}: {e}")
        for p in procs.values():
            _say(f"---- {p['name']} 日志尾部 ----\n{_tail(p['log'], 25)}")
        return 1
    finally:
        for p in procs.values():
            try:
                _stop(p)
            except Exception:
                pass
        for c in clients:
            try:
                await c.aclose()
            except Exception:
                pass
        try:
            r = _redis_client(args.redis_url)
            try:
                r.flushdb()
            finally:
                r.close()
        except Exception:
            pass
        if args.keep:
            _say(f"[清理] 保留临时目录（含子进程日志）：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="M55-A 双进程并发压测")
    parser.add_argument("--quick", action="store_true", help="冒烟规模（20 任务/50 请求，无租约阶段）")
    parser.add_argument("--tasks", type=int, default=200, help="任务提交数（全量默认 200）")
    parser.add_argument("--requests", type=int, default=500, help="网关并发请求数（全量默认 500）")
    parser.add_argument("--idem-submits", type=int, default=10, help="同幂等键任务提交次数")
    parser.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--skip-lease-probe", action="store_true", help="跳过租约恢复阶段")
    parser.add_argument("--keep", action="store_true", help="保留临时目录与日志（调试）")
    args = parser.parse_args(argv)
    if args.quick:
        args.tasks, args.requests, args.idem_submits = 20, 50, 6
        args.skip_lease_probe = True
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(run_loadtest(args))


if __name__ == "__main__":
    sys.exit(main())

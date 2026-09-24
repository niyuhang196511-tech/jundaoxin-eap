#!/usr/bin/env python3
"""EAP 一键备份 / 恢复演练（M33 · 任务组 P4 `prod-ha-`，v0.9 生产化）。

用法（仓库根执行，也可在任意目录）：
    uv run python scripts/drill_restore.py backup --out ./backups
    uv run python scripts/drill_restore.py drill --latest ./backups
    uv run python scripts/drill_restore.py drill --backup ./backups/eap-backup-xxx.db --keep

设计（docs/11-production-runbook.md §3/§4 既有规程的自动化）：
  - backup：按 EAP_DB_URL 形态自动选择备份方式——
      SQLite：sqlite3 在线备份 API（connection.backup，一致性快照，不中断写入）；
      PostgreSQL：pg_dump Custom 格式（-Fc，等价 runbook 的 docker exec pg_dump -Fc）。
  - drill：备份文件 → 临时目录还原 → Alembic `upgrade head`（确认可迁移）→
    冒烟断言（关键表可计数 ≥0、alembic_version 单头）→ 结构化报告 → 清理（--keep 保留）。
    演练全程只在临时副本 / 演练库上操作，绝不回写源库。

依赖：Python 3.12 标准库（sqlite3/subprocess/tempfile/argparse）；PostgreSQL 分支需
环境内有 pg_dump / pg_restore / psycopg（应用 venv 自带 psycopg，见 pyproject postgres extra）。
Alembic 优先用当前解释器执行；不可用时回退 `uv run --project eap alembic`。

退出码：全绿 0；任一步失败 1；用法错误 2（argparse）。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import tarfile
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]          # 仓库根（scripts/ 的上一级）
DEFAULT_EAP_DIR = ROOT / "eap"                      # alembic.ini 与 migrations/ 所在

BACKUP_GLOB = "eap-backup-*"                        # backup 子命令的输出命名（--latest 依据）
KEY_TABLES = ("tenants", "agents", "tasks")         # 冒烟断言的关键表（核心域）

# 命令超时（秒）：备份/还原 10 分钟，Alembic 迁移 5 分钟
TIMEOUT_BACKUP = 600
TIMEOUT_RESTORE = 600
TIMEOUT_ALEMBIC = 300


# ---------------------------------------------------------------- 基础设施

def _log(msg: str) -> None:
    """带时间戳的过程行（每步开始/结束各一行，便于 CI/人工查看）。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclasses.dataclass
class Step:
    """演练报告的单个步骤记录。"""
    name: str
    ok: bool = False
    seconds: float = 0.0
    detail: str = ""


class Drill:
    """步骤收集器：run() 包装单步执行（计时 + 异常捕获）；关键步骤失败由调用方短路后续。"""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self._no = 0

    def run(self, name: str, fn: Callable[[], str]) -> bool:
        """执行一步：打印 [步骤 N] 行，成功记 detail，失败记错误信息。"""
        self._no += 1
        no = self._no
        _log(f"[步骤 {no}] {name}")
        step = Step(name=name)
        t0 = time.monotonic()
        try:
            step.detail = fn() or ""
            step.ok = True
            _log(f"[步骤 {no}] 通过（{step.detail}）")
        except Exception as e:  # noqa: BLE001 —— 任何失败都要落报告而非向上中断
            step.detail = f"{type(e).__name__}: {e}"
            _log(f"[步骤 {no}] 失败：{step.detail}")
        step.seconds = round(time.monotonic() - t0, 3)
        self.steps.append(step)
        return step.ok

    @property
    def all_green(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def render(self, title: str) -> str:
        """人类可读报告；JSON 结构见 report_json()。"""
        lines = [f"== {title} ==", ""]
        for i, s in enumerate(self.steps, 1):
            flag = "[通过]" if s.ok else "[失败]"
            lines.append(f"  {flag} 步骤 {i} {s.name}  {s.seconds:.1f}s  {s.detail}")
        lines.append("")
        verdict = "全绿" if self.all_green else "存在失败"
        lines.append(f"结论：{verdict}（通过 {sum(s.ok for s in self.steps)}/{len(self.steps)} 步）")
        return "\n".join(lines)

    def report_json(self, kind: str, backup_file: str) -> dict:
        return {
            "kind": kind,
            "backup": backup_file,
            "ok": self.all_green,
            "steps": [dataclasses.asdict(s) for s in self.steps],
        }


# ---------------------------------------------------------------- URL 解析

def _db_kind(db_url: str) -> str:
    """'sqlite' | 'postgres'，按 URL scheme 判定。"""
    scheme = urlsplit(db_url).scheme.lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    if scheme.startswith("postgres"):
        return "postgres"
    raise ValueError(f"不支持的 EAP_DB_URL scheme：{scheme!r}（支持 sqlite:// 与 postgresql(+psycopg)://）")


def sqlite_path(db_url: str) -> Path:
    """sqlite URL → 文件路径。

    SQLAlchemy 语义：三斜线 = 相对路径（Windows 盘符形态视为绝对）；四斜线 = POSIX 绝对。
      sqlite:///./eap.db        → ./eap.db
      sqlite:///E:/data/eap.db  → E:/data/eap.db（Windows 绝对）
      sqlite:////abs/eap.db     → /abs/eap.db（POSIX 绝对）
    """
    rest = db_url.split("://", 1)[1]
    if rest.startswith("/"):
        rest = rest[1:]                      # 剥掉三斜线形态的第三个斜线
    if not rest:
        raise ValueError("sqlite URL 缺少路径部分")
    return Path(rest)


def _sqlite_url_of(path: Path) -> str:
    """文件路径 → sqlite URL（与 sqlite_path() 互逆；POSIX 绝对四斜线，Windows 盘符三斜线）。"""
    p = path.resolve().as_posix()
    if p.startswith("/"):
        return "sqlite://" + p               # POSIX 绝对 → 四斜线
    return "sqlite:///" + p


def pg_libpq_url(db_url: str) -> str:
    """SQLAlchemy URL（postgresql+psycopg://…）→ libpq 连接串（pg_dump/pg_restore 直接可用）。"""
    scheme = urlsplit(db_url).scheme.lower()
    return db_url.replace(f"{scheme}://", "postgresql://", 1)


def resolve_db_url(cli_value: str | None) -> str:
    """备份源库 URL：--db-url > EAP_DB_URL > 开发默认（仓库布局下定位 eap/eap.db）。"""
    if cli_value:
        return cli_value
    env = os.environ.get("EAP_DB_URL", "").strip()
    if env:
        return env
    dev_db = DEFAULT_EAP_DIR / "eap.db"
    if dev_db.exists():
        return _sqlite_url_of(dev_db)
    return "sqlite:///./eap.db"


# ---------------------------------------------------------------- backup 子命令


# ---------------------------------------------------------------- 媒体目录（M47-C）

MEDIA_MANIFEST = "MANIFEST.sha256"


def _media_tar(media_dir: str, out_path: Path) -> Path:
    """把 EAP_MEDIA_DIR 打包为 tar.gz + sha256 清单（MANIFEST.sha256 记录每个文件的哈希）。

    还原时以清单复核完整性——消除「库恢复成功但媒体文件丢失/漂移」的版本错位。
    """
    src = Path(media_dir)
    if not src.is_dir():
        raise NotADirectoryError(f"媒体目录不存在：{src}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with tarfile.open(out_path, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=f.relative_to(src).as_posix())
    lines = []
    for f in files:
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{h}  {f.relative_to(src).as_posix()}")
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    _log(f"[media] 打包 {len(files)} 个文件 → {out_path}（清单 {sidecar.name}）")
    return out_path


def _verify_media_tar(tar_path: Path, dest: Path) -> str:
    """解包媒体备份到 dest 并按 sha256 sidecar（tar 同名 + .sha256）复核完整性；返回摘要文本。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")  # 成员路径校验交给标准库 data filter（防路径穿越）
    manifest = Path(str(tar_path) + ".sha256")  # 清单是 tar 旁的 sidecar 文件（backup 时写在其旁）
    if not manifest.is_file():
        raise RuntimeError(f"媒体备份缺少清单 {manifest.name}，无法校验完整性")
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        h, rel = line.split("  ", 1)
        f = dest / rel
        if not f.is_file():
            raise RuntimeError(f"媒体备份缺文件：{rel}")
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != h:
            raise RuntimeError(f"媒体文件哈希不符：{rel}")
        checked += 1
    return f"{checked} 个媒体文件完整性校验通过"


def cmd_backup(args: argparse.Namespace) -> int:
    db_url = resolve_db_url(args.db_url)
    kind = _db_kind(db_url)
    out = Path(args.out)
    target: Path
    _log(f"备份源库：{db_url}（形态判定：{kind}）")
    t0 = time.monotonic()

    if kind == "sqlite":
        src = sqlite_path(db_url)
        if not src.exists():
            _log(f"[失败] 源库不存在：{src}")
            return 1
        # --out 为已存在目录 → 目录内按时间戳命名；否则视为文件路径
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = out / f"eap-backup-{stamp}.db" if out.is_dir() else out
        target.parent.mkdir(parents=True, exist_ok=True)
        # 在线一致性备份：sqlite3 backup API（source.backup(dest)，对 WAL 库同样安全）
        with sqlite3.connect(src) as scon, sqlite3.connect(target) as dcon:
            scon.backup(dcon)
    else:
        exe = shutil.which("pg_dump")
        if not exe:
            _log("[失败] 未找到 pg_dump（PostgreSQL 分支需要客户端工具，"
                 "或改在宿主/容器内按 runbook §3 执行）")
            return 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = out / f"eap-backup-{stamp}.dump" if out.is_dir() else out
        target.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [exe, "--format=custom", "--no-owner", "--no-privileges",
             f"--file={target}", f"--dbname={pg_libpq_url(db_url)}"],
            capture_output=True, text=True, timeout=TIMEOUT_BACKUP)
        if r.returncode != 0:
            _log(f"[失败] pg_dump 退出码 {r.returncode}：{(r.stderr or r.stdout).strip()[:500]}")
            return 1

    size = target.stat().st_size
    if size == 0:
        _log(f"[失败] 备份产物为空：{target}")
        return 1
    _log(f"[backup] 写出：{target}（{size} 字节，耗时 {time.monotonic() - t0:.1f}s）")
    if args.media_dir:
        m0 = time.monotonic()
        _media_tar(args.media_dir, out / f"eap-media-{stamp}.tar.gz")
        _log(f"[media] 耗时 {time.monotonic() - m0:.1f}s")
    print(str(target))  # 末行单独输出产物路径，供脚本/CI 消费
    return 0


# ---------------------------------------------------------------- drill 子命令

def _resolve_backup(args: argparse.Namespace) -> Path:
    """--backup 显式文件 或 --latest 目录内最近一份；初检存在且非空。"""
    if args.backup:
        p = Path(args.backup)
    elif args.latest:
        d = Path(args.latest)
        if not d.is_dir():
            raise NotADirectoryError(f"--latest 目录不存在：{d}")
        cands = sorted(d.glob(BACKUP_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise FileNotFoundError(f"{d} 下没有匹配 {BACKUP_GLOB} 的备份文件")
        p = cands[0]
    else:  # argparse mutually-exclusive required 已挡住，防御性兜底
        raise ValueError("必须提供 --backup <文件> 或 --latest <目录> 之一")
    if not p.is_file():
        raise FileNotFoundError(f"备份文件不存在：{p}")
    if p.stat().st_size == 0:
        raise RuntimeError(f"备份文件为空：{p}")
    return p


def _alembic_command(eap_dir: Path) -> list[str]:
    """Alembic 运行方式：当前解释器有 alembic 用之；否则回退 uv run --project eap。"""
    if importlib.util.find_spec("alembic") is not None:
        return [sys.executable, "-m", "alembic"]
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(eap_dir), "alembic"]
    raise RuntimeError("当前解释器无 alembic 且未找到 uv：无法执行迁移演练"
                       "（请改在 eap 项目 venv 内运行本脚本，或安装 uv）")


def _alembic_upgrade(eap_dir: Path, db_url: str) -> str:
    """对还原库执行 alembic upgrade head（独立进程、注入 EAP_DB_URL，确认可迁移）。"""
    if not (eap_dir / "alembic.ini").is_file():
        raise FileNotFoundError(f"未找到 {eap_dir / 'alembic.ini'}（--eap-dir 应指向 eap/ 项目根）")
    cmd = _alembic_command(eap_dir)
    env = {**os.environ, "EAP_DB_URL": db_url}
    r = subprocess.run(cmd + ["upgrade", "head"], cwd=str(eap_dir), env=env,
                       capture_output=True, text=True, timeout=TIMEOUT_ALEMBIC)
    tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    detail = tail[-1][:200] if tail else ""
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade head 退出码 {r.returncode}：{detail}")
    return detail or "已在 head（无待执行迁移）"


def _smoke_sqlite(restored: Path) -> str:
    """SQLite 冒烟断言：integrity_check + 关键表存在且可计数（≥0）+ alembic_version 单头。"""
    con = sqlite3.connect(f"file:{restored.as_posix()}?mode=ro", uri=True)
    try:
        chk = con.execute("PRAGMA integrity_check").fetchone()[0]
        if chk != "ok":
            raise RuntimeError(f"integrity_check={chk}")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts: dict[str, int] = {}
        for t in (*KEY_TABLES, "alembic_version"):
            if t not in tables:
                raise RuntimeError(f"关键表缺失：{t}")
            counts[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        if counts["alembic_version"] != 1:
            raise RuntimeError(f"alembic_version 应单头，实际 {counts['alembic_version']} 行")
        kv = ", ".join(f"{t}={c}" for t, c in counts.items() if t != "alembic_version")
        return f"integrity=ok，alembic_version 单头，关键表行数 {kv}"
    finally:
        con.close()


def _smoke_pg(restore_url: str) -> str:
    """PostgreSQL 冒烟断言（psycopg 直连；断言项与 SQLite 分支对齐）。"""
    try:
        import psycopg  # noqa: PLC0415 —— 仅 PostgreSQL 演练时需要
    except ImportError as e:
        raise RuntimeError("psycopg 不可用（uv sync --extra postgres 后再演练）") from e
    with psycopg.connect(pg_libpq_url(restore_url), connect_timeout=10) as con:
        counts: dict[str, int] = {}
        for t in (*KEY_TABLES, "alembic_version"):
            counts[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # 表缺失即失败
        if counts["alembic_version"] != 1:
            raise RuntimeError(f"alembic_version 应单头，实际 {counts['alembic_version']} 行")
        kv = ", ".join(f"{t}={c}" for t, c in counts.items() if t != "alembic_version")
        return f"alembic_version 单头，关键表行数 {kv}"


def cmd_drill(args: argparse.Namespace) -> int:
    eap_dir = Path(args.eap_dir).resolve()
    drill = Drill()
    ctx: dict[str, object] = {}
    tmp: Path | None = None

    # 步骤 1：定位备份文件（.dump → PostgreSQL Custom 格式；其余按 SQLite）
    def _loc() -> str:
        p = _resolve_backup(args)
        ctx["backup"] = p
        ctx["kind"] = "postgres" if p.suffix == ".dump" else "sqlite"
        return f"{p.name}（{p.stat().st_size} 字节，{ctx['kind']}）"
    if not drill.run("定位备份文件", _loc):
        return _finish(drill, args, None, "unknown", Path(""))

    kind = str(ctx["kind"])
    backup: Path = ctx["backup"]  # type: ignore[assignment]

    # 步骤 2：临时演练目录
    def _tmp() -> str:
        nonlocal tmp
        tmp = Path(tempfile.mkdtemp(prefix="eap-drill-"))
        return str(tmp)
    if not drill.run("创建临时演练目录", _tmp):
        return _finish(drill, args, tmp, kind, backup)
    # 步骤 2.5（M47-C）：媒体备份包完整性校验（--media 提供时；与库还原演练解耦为独立步骤）
    if getattr(args, "media", None):
        def _media() -> str:
            media_dest = tmp / "media"  # type: union-attr —— 步骤 2 已保证非空
            summary = _verify_media_tar(Path(args.media), media_dest)
            ctx["media_verified"] = summary
            return f"{args.media}：{summary}"
        if not drill.run("媒体备份完整性校验", _media):
            return _finish(drill, args, tmp, kind, backup)


    # 步骤 3：还原（SQLite 直接还原副本；PostgreSQL pg_restore 到演练库）
    if kind == "sqlite":
        def _restore() -> str:
            restored = tmp / "restored.db"  # type: union-attr —— 步骤 2 已保证非空
            shutil.copy2(backup, restored)
            # 还原即校验：能以只读打开且 sqlite_master 可查询——坏文件在此得到清晰报错，
            # 而不是等到 Alembic 阶段抛 DatabaseError
            try:
                con = sqlite3.connect(f"file:{restored.as_posix()}?mode=ro", uri=True)
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
                con.close()
            except sqlite3.DatabaseError as e:
                raise RuntimeError(f"备份不是有效的 SQLite 数据库：{e}") from e
            ctx["restored"] = restored
            ctx["restore_url"] = _sqlite_url_of(restored)
            return f"副本 → {restored}"
    else:
        def _restore() -> str:
            rd = args.restore_db
            if not rd:
                raise ValueError("PostgreSQL 演练必须提供 --restore-db <演练库>（库名或完整连接串）")
            if "://" in rd:
                restore_url = rd
            else:  # 裸库名：宿主/凭据沿用 EAP_DB_URL，仅替换库
                base = os.environ.get("EAP_DB_URL", "").strip()
                if not base:
                    raise ValueError("--restore-db 为裸库名时需要 EAP_DB_URL 提供宿主与凭据")
                parts = urlsplit(base)
                restore_url = urlunsplit((parts.scheme, parts.netloc, f"/{rd.lstrip('/')}", "", ""))
            exe = shutil.which("pg_restore")
            if not exe:
                raise RuntimeError("未找到 pg_restore（PostgreSQL 客户端工具）")
            # --clean --if-exists：与 runbook §4 恢复规程一致（演练库可重复使用）
            r = subprocess.run(
                [exe, "--clean", "--if-exists", "--no-owner", "--no-privileges",
                 f"--dbname={pg_libpq_url(restore_url)}", str(backup)],
                capture_output=True, text=True, timeout=TIMEOUT_RESTORE)
            if r.returncode != 0:
                raise RuntimeError(f"pg_restore 退出码 {r.returncode}："
                                   f"{(r.stderr or r.stdout).strip()[:500]}")
            ctx["restore_url"] = restore_url
            return f"pg_restore → {restore_url}"
    if not drill.run("还原备份", _restore):
        return _finish(drill, args, tmp, kind, backup)

    # 步骤 4：Alembic upgrade head（确认还原库可迁移）
    if not drill.run("Alembic upgrade head（可迁移性）",
                     lambda: _alembic_upgrade(eap_dir, str(ctx["restore_url"]))):
        return _finish(drill, args, tmp, kind, backup)

    # 步骤 5：冒烟断言（关键表行数 ≥0、alembic_version 单头）
    if kind == "sqlite":
        fn: Callable[[], str] = lambda: _smoke_sqlite(ctx["restored"])  # type: ignore[arg-type]  # noqa: E731
    else:
        fn = lambda: _smoke_pg(str(ctx["restore_url"]))  # noqa: E731
    drill.run("冒烟断言", fn)

    return _finish(drill, args, tmp, kind, backup)


def _finish(drill: Drill, args: argparse.Namespace, tmp: Path | None,
            kind: str, backup: Path) -> int:
    """出报告（人类可读 + 可选 JSON）→ 清理 → 退出码。"""
    print()
    print(drill.render(f"EAP 恢复演练报告（{kind}）"))
    if getattr(args, "report_json", None):
        Path(args.report_json).write_text(
            json.dumps(drill.report_json(kind, str(backup)), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"JSON 报告：{args.report_json}")
    rc = 0 if drill.all_green else 1
    # 清理策略：成功且未 --keep → 自动清理；失败或 --keep → 保留现场便于排查
    if tmp is not None:
        if args.keep or not drill.all_green:
            _log(f"临时目录保留（{'--keep' if args.keep else '演练未全绿，保留现场'}）：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            _log(f"已清理临时目录：{tmp}")
    _log(f"演练结束，退出码 {rc}")
    return rc


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drill_restore.py",
        description="EAP 一键备份 / 恢复演练（SQLite 在线备份 + PostgreSQL pg_dump/pg_restore；"
                    "演练在临时副本/演练库上进行，绝不回写源库）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  # 备份开发库（默认定位 eap/eap.db，或读 EAP_DB_URL）
  uv run python scripts/drill_restore.py backup --out ./backups

  # 备份 PostgreSQL 生产库（读 EAP_DB_URL）
  EAP_DB_URL=postgresql+psycopg://eap:pass@pg:5432/eap \\
      uv run python scripts/drill_restore.py backup --out /backup

  # 恢复演练：取最近一份备份 → 临时目录还原 → alembic upgrade head → 冒烟断言
  uv run python scripts/drill_restore.py drill --latest ./backups

  # PostgreSQL 演练（演练库需可写入；--clean --if-exists 语义见 runbook §4）
  EAP_DB_URL=postgresql+psycopg://eap:pass@pg:5432/eap \\
      uv run python scripts/drill_restore.py drill --latest /backup --restore-db drill_eap

每步打印 [步骤 N] 行；退出码：全绿 0、任一步失败 1，便于 CI/人工查看。
月度定时演练（crontab）：0 5 1 * * cd /opt/eap && uv run python scripts/drill_restore.py drill --latest /backup --restore-db drill_eap
""")
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("backup", help="备份源库（按 EAP_DB_URL 形态自动选择 SQLite/PostgreSQL 方式）",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pb.add_argument("--out", default="./backups",
                    help="输出目录（默认 ./backups）或完整文件路径；目录内命名 eap-backup-<时间戳>.db|.dump")
    pb.add_argument("--db-url", default=None,
                    help="覆盖源库连接串（默认取 EAP_DB_URL；未配置且为仓库布局时自动用 eap/eap.db）")
    pb.add_argument("--media-dir", default=None,
                    help="M47-C：一并打包 EAP_MEDIA_DIR 媒体目录（tar.gz + sha256 清单，与库备份同时间戳）")
    pb.set_defaults(fn=cmd_backup)

    pd = sub.add_parser("drill", help="恢复演练：还原 → 迁移 → 冒烟断言 → 报告 → 清理",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = pd.add_mutually_exclusive_group(required=True)
    src.add_argument("--backup", help="备份文件路径（.dump 按 PostgreSQL 处理，其余按 SQLite）")
    src.add_argument("--latest", help=f"目录：自动取其中最近修改的 {BACKUP_GLOB} 文件")
    pd.add_argument("--restore-db", default=None,
                    help="PostgreSQL 演练库名或完整连接串（pg_restore 目标；SQLite 分支忽略）")
    pd.add_argument("--eap-dir", default=str(DEFAULT_EAP_DIR),
                    help=f"eap 项目根（含 alembic.ini，默认 {DEFAULT_EAP_DIR}）")
    pd.add_argument("--keep", action="store_true", help="保留临时演练目录（默认成功后清理；失败时总是保留）")
    pd.add_argument("--media", default=None,
                    help="M47-C：媒体备份包路径（backup --media-dir 产物）——演练增加完整性校验步骤")
    pd.add_argument("--report-json", default=None, help="将结构化报告写入 JSON 文件")
    pd.set_defaults(fn=cmd_drill)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台中文
    except Exception:  # noqa: BLE001 —— 非 tty 场景可能无 reconfigure
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, FileNotFoundError, NotADirectoryError, RuntimeError) as e:
        _log(f"[失败] {e}")
        return 1
    except subprocess.TimeoutExpired as e:
        _log(f"[失败] 命令超时（{getattr(e, 'timeout', '?')}s）：{e.cmd}")
        return 1
    except KeyboardInterrupt:
        _log("[失败] 用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

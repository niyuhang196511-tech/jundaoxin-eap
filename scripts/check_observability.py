#!/usr/bin/env python3
"""观测配置一致性检查（M49-D · P2 #23 Loki 生产调余 + #24 alertmanager 联调的可代码化半区）。

用法（仓库根执行；路径按脚本位置解析，与 CWD 无关）：
    uv run --with pyyaml python scripts/check_observability.py            # 纯 YAML/结构校验
    uv run --with pyyaml python scripts/check_observability.py --docker   # + 容器内 promtool/amtool/loki 深度校验

检查项：
  [yaml-parse]  deploy/observability/*.yml + deploy/prometheus.yml
                + deploy/prometheus-alerts.yml 全部可解析；
  [结构断言]    ① prometheus.yml rule_files 指向的文件存在（经 compose prometheus
                   服务的 volume 挂载映射解析到宿主文件）；
                ② alerting 目标主机是 compose 栈内服务（alertmanager 可达）；
                ③ compose 存在 prometheus 服务、镜像为 prom/prometheus、挂载了
                   deploy/prometheus.yml 主配置与规则目录，且接入 external 后端网络
                   （跨 compose 项目抓 eap:8300 的前提）；
                ④ loki-config.yml 含 retention 调优（limits_config.retention_period
                   + compactor.retention_enabled）与 allow_structured_metadata: true
                   （M54-C），且 compose loki 服务实际挂载它；
                ⑤ alertmanager 路由树（含嵌套）引用的 receiver 均有定义；
                ⑥ 告警规则 expr 非空、alert 名全局唯一；
                ⑦ promtail 采数管线（M54-C trace_id 根治的双向断言，防漂移回退）：
                   trace_id 不得被 labels stage 提升为标签（高基数反模式），必须出现在
                   structured_metadata stage（不进索引，LogQL | trace_id="..." 仍可过滤）；
                   level 保留为标签（低基数正当，按级别检索依赖它）；
  [--docker]    promtool check config（连带规则文件）/ amtool check-config /
                loki -verify-config；docker 不可用或镜像拉取失败打印 SKIP（不计失败）。

退出码：0 = 无 FAIL（SKIP 不影响）；1 = 至少一项 FAIL。依赖 pyyaml；docker CLI 可选。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少 pyyaml：请用 `uv run --with pyyaml python scripts/check_observability.py` 执行")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
OBS = DEPLOY / "observability"
COMPOSE = OBS / "docker-compose.observability.yml"
PROM_YML = DEPLOY / "prometheus.yml"
ALERTS_YML = DEPLOY / "prometheus-alerts.yml"
LOKI_YML = OBS / "loki-config.yml"
PROMTAIL_YML = OBS / "promtail-config.yml"
AM_YML = OBS / "alertmanager.yml"

PROM_CONTAINER_PATH = "/etc/prometheus/prometheus.yml"
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class CheckError(Exception):
    """结构断言失败。"""


class CheckSkip(Exception):
    """可选深度校验无法执行（docker 缺失/镜像拉不动），不计失败。"""


RESULTS: list[tuple[str, str, str]] = []


def rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def record(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, detail))


def run_check(name: str, fn) -> None:
    try:
        detail = fn() or "ok"
        record(PASS, name, detail)
    except CheckSkip as exc:
        record(SKIP, name, str(exc))
    except CheckError as exc:
        record(FAIL, name, str(exc))
    except Exception as exc:  # noqa: BLE001 —— 检查脚本自身异常也应可见并计 FAIL
        record(FAIL, name, f"检查脚本异常: {exc!r}")


# ---------------------------------------------------------------- YAML 加载

def load_all_yaml() -> tuple[list[Path], dict[Path, object], list[str]]:
    files = sorted(OBS.glob("*.yml")) + [PROM_YML, ALERTS_YML]
    # 去重（prometheus-alerts.yml 不在 observability/ 下，正常不重复）并保持顺序
    seen: set[Path] = set()
    files = [f for f in files if not (f in seen or seen.add(f))]
    docs: dict[Path, object] = {}
    problems: list[str] = []
    for f in files:
        try:
            docs[f] = yaml.safe_load(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            problems.append(f"{rel(f)}: 文件不存在")
        except yaml.YAMLError as exc:
            first = str(exc).splitlines()[0]
            problems.append(f"{rel(f)}: YAML 解析失败 → {first}")
    return files, docs, problems


def get_doc(docs: dict, path: Path, what: str):
    doc = docs.get(path)
    if doc is None:
        raise CheckError(f"{what}（{rel(path)}）缺失或不可解析")
    if not isinstance(doc, dict):
        raise CheckError(f"{what}（{rel(path)}）不是映射结构")
    return doc


def compose_services(docs: dict) -> dict:
    compose = get_doc(docs, COMPOSE, "observability compose")
    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        raise CheckError("compose 缺少 services")
    return services


def compose_networks(docs: dict) -> dict:
    compose = get_doc(docs, COMPOSE, "observability compose")
    networks = compose.get("networks")
    return networks if isinstance(networks, dict) else {}


def bind_mounts(svc: dict) -> list[tuple[Path, str]]:
    """解析服务 volumes 的 bind 挂载 → [(宿主路径(相对 compose 目录解析), 容器路径)]。

    命名卷（无 : 或源不是路径样式）跳过；兼容 Windows 盘符绝对源。
    """
    out: list[tuple[Path, str]] = []
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):  # compose 长语法
            if v.get("type") == "bind" and v.get("source") and v.get("target"):
                out.append((Path(str(v["source"])), str(v["target"])))
            continue
        parts = str(v).split(":")
        if len(parts) >= 4 and re.match(r"^[A-Za-z]$", parts[0]):
            parts = [f"{parts[0]}:{parts[1]}"] + parts[2:]  # 盘符源被 : 切开，拼回
        if len(parts) < 2:
            continue
        src, dst = parts[0], parts[1]
        if src.startswith(("./", "../", "/", "\\")) or DRIVE_RE.match(src):
            host = Path(src)
            if not host.is_absolute():
                host = (COMPOSE.parent / src).resolve()
            out.append((host, dst))
    return out


def mount_targets(svc: dict) -> set[str]:
    """服务全部 volume 的容器路径（含命名卷/匿名卷——数据卷断言用，不要求宿主侧存在）。"""
    out: set[str] = set()
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            if v.get("target"):
                out.add(str(v["target"]))
            continue
        parts = str(v).split(":")
        if len(parts) >= 4 and re.match(r"^[A-Za-z]$", parts[0]):
            parts = [f"{parts[0]}:{parts[1]}"] + parts[2:]
        if len(parts) == 1:
            out.add(parts[0])          # 匿名卷（如 "- /prometheus"）
        elif len(parts) >= 2:
            out.add(parts[1])
    return out


def command_str(svc: dict) -> str:
    cmd = svc.get("command")
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return ""


# ---------------------------------------------------------------- 结构断言

def check_prometheus_rule_files(docs: dict) -> str:
    prom = get_doc(docs, PROM_YML, "prometheus 主配置")
    rule_files = prom.get("rule_files") or []
    if not isinstance(rule_files, list) or not rule_files:
        raise CheckError("rule_files 缺失或为空——告警规则不会被加载")
    svc = compose_services(docs).get("prometheus")
    if svc is None:
        raise CheckError("compose 无 prometheus 服务，无法解析 rule_files 的容器挂载")
    mounts = bind_mounts(svc)
    file_map = {dst: host for host, dst in mounts}
    details = []
    for rf in rule_files:
        rf = str(rf)
        # 容器内 POSIX 绝对路径判定用字符串前缀——Windows 上 Path("/etc/x").is_absolute()
        # 为 False（无盘符），不能用它区分容器路径与宿主相对路径
        if rf.startswith("/") or rf.startswith("\\") or DRIVE_RE.match(rf):
            host = file_map.get(rf)
            if host is not None:  # 文件级挂载（本仓库形态）
                if not host.is_file():
                    raise CheckError(f"rule_files {rf} 对应宿主文件不存在: {rel(host)}")
                details.append(f"{rf} → {rel(host)}")
                continue
            # 目录级挂载（glob 形态 rule_files，如 /etc/prometheus/rules/*.yml）
            handled = False
            for h, dst in mounts:
                prefix = dst.rstrip("/")
                if not (dst.endswith("/") or rf.startswith(prefix + "/")):
                    continue
                rel_part = rf[len(prefix) + 1:]
                if any(ch in rel_part for ch in "*?["):
                    matches = sorted(h.glob(rel_part)) if h.is_dir() else []
                    if not matches:
                        raise CheckError(f"rule_files {rf} 在挂载目录 {rel(h)} 下无匹配文件")
                    details.append(f"{rf} → {rel(h)}/{rel_part}（{len(matches)} 个匹配）")
                else:
                    cand = h / rel_part
                    if not cand.is_file():
                        raise CheckError(f"rule_files {rf} 对应宿主文件不存在: {rel(cand)}")
                    details.append(f"{rf} → {rel(cand)}")
                handled = True
                break
            if not handled:
                raise CheckError(f"rule_files {rf} 未被 compose prometheus 服务任何 volume 挂载")
        else:
            host = (ROOT / rf).resolve()
            if not host.is_file():
                raise CheckError(f"rule_files {rf}（宿主相对路径）不存在: {rel(host)}")
            details.append(f"{rf} → {rel(host)}")
    return f"{len(rule_files)} 条 rule_files 全部落到存在的宿主文件: {'; '.join(details)}"


def check_prometheus_alerting(docs: dict) -> str:
    prom = get_doc(docs, PROM_YML, "prometheus 主配置")
    alerting = prom.get("alerting") or {}
    ams = alerting.get("alertmanagers") or []
    if not ams:
        raise CheckError("alerting.alertmanagers 缺失——告警无处推送")
    targets: list[str] = []
    for am in ams:
        for sc in (am or {}).get("static_configs") or []:
            targets += [str(t) for t in sc.get("targets") or []]
    if not targets:
        raise CheckError("alerting.alertmanagers 无 static_configs.targets")
    svcs = compose_services(docs)
    for t in targets:
        host = t.split(":")[0]
        if host not in svcs:
            raise CheckError(f"alerting 目标 {t} 的主机 {host!r} 不是 compose 栈内服务（无法解析）")
    return f"alerting 目标 {targets} 均对应 compose 服务"


def check_compose_prometheus_service(docs: dict) -> str:
    svcs = compose_services(docs)
    svc = svcs.get("prometheus")
    if svc is None:
        raise CheckError("compose 缺少 prometheus 服务（#24 联调核心缺口未补）")
    image = str(svc.get("image") or "")
    if not image.startswith("prom/prometheus"):
        raise CheckError(f"prometheus 服务镜像应为 prom/prometheus:*，实际 {image!r}")
    mounts = bind_mounts(svc)
    main_mount = [h for h, c in mounts if c == PROM_CONTAINER_PATH]
    if not main_mount:
        raise CheckError(f"prometheus 服务未挂载主配置到 {PROM_CONTAINER_PATH}")
    host_main = main_mount[0].resolve()
    if host_main != PROM_YML.resolve():
        raise CheckError(f"挂载到 {PROM_CONTAINER_PATH} 的宿主文件是 {rel(host_main)}，应为 {rel(PROM_YML)}")
    if not host_main.is_file():
        raise CheckError(f"主配置宿主文件不存在: {rel(host_main)}")
    if not any(c.startswith("/etc/prometheus/rules") for _, c in mounts):
        raise CheckError("prometheus 服务未挂载规则目录 /etc/prometheus/rules（rule_files 将落空）")
    if "/prometheus" not in mount_targets(svc):  # 命名卷，不在 bind_mounts 里
        raise CheckError("prometheus 服务未挂载 /prometheus 数据卷（TSDB 重启即丢）")
    cmd = command_str(svc)
    if cmd and "config.file" in cmd and PROM_CONTAINER_PATH not in cmd:
        raise CheckError(f"command 的 --config.file 与主配置挂载点不一致: {cmd}")
    # 跨 compose 抓取前提：prometheus 必须接入声明为 external 的后端网络
    nets = svc.get("networks") or []
    if isinstance(nets, dict):
        nets = list(nets)
    top_nets = compose_networks(docs)
    external_joined = [
        n for n in nets
        if isinstance(top_nets.get(str(n)), dict) and top_nets[str(n)].get("external")
    ]
    if not external_joined:
        raise CheckError(
            "prometheus 服务未接入任何 external 后端网络——eap 服务只在后端栈内网 expose，"
            "不共网将抓不到 eap:8300"
        )
    return (
        f"image={image}，主配置/规则/数据卷挂载齐全，接入 external 网络 {external_joined}（"
        f"name={top_nets[str(external_joined[0])].get('name', '(项目默认名)')}）"
    )


def check_loki_config(docs: dict) -> str:
    cfg = get_doc(docs, LOKI_YML, "loki 自定义配置")
    limits = cfg.get("limits_config") or {}
    retention = limits.get("retention_period")
    if not retention:
        raise CheckError("limits_config.retention_period 未配置——回到内置默认的永久保留")
    # M54-C：trace_id 已采为 structured metadata（不进索引），此项须显式 true——
    # Loki 3.x 默认 true，但被上游/误改回 false 时含 metadata 的推送会被拒收
    if limits.get("allow_structured_metadata") is not True:
        raise CheckError(
            "limits_config.allow_structured_metadata 未显式为 true——structured metadata "
            "写入会被拒收（M54-C：promtail 把 trace_id 采为 metadata 依赖此项）"
        )
    compactor = cfg.get("compactor") or {}
    if not compactor.get("retention_enabled"):
        raise CheckError("compactor.retention_enabled 非 true——retention_period 不会真正删除 chunk")
    if not compactor.get("delete_request_store"):
        raise CheckError("compactor.delete_request_store 未配置——Loki 3.x 删除无处落地")
    svcs = compose_services(docs)
    svc = svcs.get("loki")
    if svc is None:
        raise CheckError("compose 缺少 loki 服务")
    cmd = command_str(svc)
    if "config.file=" not in cmd:
        raise CheckError("loki 服务 command 未指定 -config.file（仍会用镜像内置配置）")
    target = cmd.split("config.file=", 1)[1].split()[0].strip()
    mounts = bind_mounts(svc)
    if not any(c == target and h.resolve() == LOKI_YML.resolve() for h, c in mounts):
        raise CheckError(f"loki 服务未把 {rel(LOKI_YML)} 挂到 command 指定的 {target}")
    if "/loki" not in mount_targets(svc):  # 命名卷，不在 bind_mounts 里
        raise CheckError("loki 服务未挂载 /loki 数据卷（与 common.path_prefix 存储路径不符）")
    prefix = str((cfg.get("common") or {}).get("path_prefix") or "")
    if prefix and not prefix.startswith("/loki"):
        raise CheckError(f"common.path_prefix={prefix} 与数据卷挂载点 /loki 不一致")
    return (
        f"retention_period={retention}，allow_structured_metadata=true，"
        f"compactor.retention_enabled=true，"
        f"delete_request_store={compactor['delete_request_store']}，挂载 {target} 与 command 一致"
    )


def pipeline_stage_keys(scrape: dict, stage_kind: str) -> set[str]:
    """收集单个 scrape job 管线中指定 stage（labels/structured_metadata）的全部键。

    promtail 管线 stage 是单键映射（如 {"labels": {"level": None}}）；非映射/多键
    形态跳过（不属本检查范围）。
    """
    keys: set[str] = set()
    for stage in scrape.get("pipeline_stages") or []:
        if not isinstance(stage, dict):
            continue
        for kind, body in stage.items():
            if kind == stage_kind and isinstance(body, dict):
                keys |= set(body.keys())
    return keys


def check_promtail_trace_id(docs: dict) -> str:
    """M54-C 双向断言：trace_id 不得作标签提取，必须在 structured_metadata stage。

    背景：trace_id 每请求唯一，作 Loki 标签会为每条 trace 建一个流，索引基数随
    流量线性增长（TSDB 撑爆/慢查询）；structured metadata 不进索引，LogQL 行过滤
    `| trace_id="..."` 对其同样生效。level 低基数，保留为标签（按级别检索依赖）。
    """
    cfg = get_doc(docs, PROMTAIL_YML, "promtail 配置")
    scrapes = cfg.get("scrape_configs") or []
    if not isinstance(scrapes, list) or not scrapes:
        raise CheckError("scrape_configs 缺失或为空——promtail 无任何采数目标")
    label_keys: set[str] = set()
    meta_keys: set[str] = set()
    for sc in scrapes:
        if not isinstance(sc, dict):
            raise CheckError("scrape_configs 中存在非映射条目")
        label_keys |= pipeline_stage_keys(sc, "labels")
        meta_keys |= pipeline_stage_keys(sc, "structured_metadata")
    if "trace_id" in label_keys:
        raise CheckError(
            "trace_id 仍被 labels stage 提升为标签（高基数反模式：每请求一个流，索引膨胀）"
            "——M54-C 已迁移为 structured_metadata，禁止回退"
        )
    if "trace_id" not in meta_keys:
        raise CheckError(
            'trace_id 未出现在任何 structured_metadata stage——M54-C 要求采为 metadata'
            '（不进索引，LogQL | trace_id="..." 过滤）'
        )
    if "level" not in label_keys:
        raise CheckError("level 未作为标签提取——低基数正当标签，按级别检索依赖它")
    return (
        f"labels={sorted(label_keys)}（无 trace_id），structured_metadata={sorted(meta_keys)}"
        "——trace_id 已出索引，level 保留标签（M54-C）"
    )


def collect_route_receivers(route: object, acc: set[str]) -> None:
    if not isinstance(route, dict):
        return
    if route.get("receiver"):
        acc.add(str(route["receiver"]))
    for sub in route.get("routes") or []:
        collect_route_receivers(sub, acc)


def check_alertmanager_routes(docs: dict) -> str:
    am = get_doc(docs, AM_YML, "alertmanager 配置")
    receivers = am.get("receivers") or []
    defined = {str(r.get("name")) for r in receivers if isinstance(r, dict) and r.get("name")}
    if not defined:
        raise CheckError("receivers 缺失或均无 name")
    route = am.get("route")
    if not isinstance(route, dict):
        raise CheckError("route 缺失")
    if not route.get("receiver"):
        raise CheckError("route 根节点未指定默认 receiver")
    referenced: set[str] = set()
    collect_route_receivers(route, referenced)
    missing = referenced - defined
    if missing:
        raise CheckError(f"路由树引用了未定义的 receiver: {sorted(missing)}")
    return f"路由树引用 {sorted(referenced)} ⊆ 已定义 {sorted(defined)}"


def check_alert_rules(docs: dict) -> str:
    data = get_doc(docs, ALERTS_YML, "告警规则文件")
    groups = data.get("groups") or []
    if not groups:
        raise CheckError("groups 为空")
    seen: dict[str, str] = {}
    n_alerts = 0
    for g in groups:
        if not isinstance(g, dict):
            raise CheckError("groups 中存在非映射条目")
        gname = str(g.get("name") or "")
        if not gname:
            raise CheckError("存在无 name 的规则组")
        for r in g.get("rules") or []:
            if not isinstance(r, dict) or "alert" not in r:
                continue  # recording rule 不在本检查范围
            n_alerts += 1
            name = str(r.get("alert") or "")
            if not name:
                raise CheckError(f"组 {gname} 存在无 alert 名的告警规则")
            expr = r.get("expr")
            if not (isinstance(expr, str) and expr.strip()):
                raise CheckError(f"告警 {name} 的 expr 为空")
            if name in seen:
                raise CheckError(f"alert 名重复: {name}（组 {seen[name]} 与 {gname}）")
            seen[name] = gname
    if n_alerts == 0:
        raise CheckError("未找到任何告警规则")
    return f"{len(groups)} 组 / {n_alerts} 条 alert，expr 全非空，alert 名全局唯一"


# ---------------------------------------------------------------- --docker 深度校验

DOCKER_RUN_TIMEOUT = 180
DOCKER_PULL_TIMEOUT = 600


def docker_ready() -> str:
    """docker 可用返回版本串；不可用抛 CheckSkip。"""
    if shutil.which("docker") is None:
        raise CheckSkip("未找到 docker CLI")
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckSkip(f"docker 探测失败: {exc}") from exc
    if r.returncode != 0:
        reason = (r.stderr or r.stdout or "daemon 无响应").strip().splitlines()[0]
        raise CheckSkip(f"docker daemon 不可用: {reason}")
    return r.stdout.strip()


def ensure_image(image: str) -> None:
    """镜像不在本地则拉取；拉取失败抛 CheckSkip（如实报告原因，不计 FAIL）。"""
    try:
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True, timeout=60).returncode == 0:
            return
        r = subprocess.run(["docker", "pull", image], capture_output=True, text=True,
                           timeout=DOCKER_PULL_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise CheckSkip(f"镜像 {image} 拉取超时（>{DOCKER_PULL_TIMEOUT}s）") from exc
    except OSError as exc:
        raise CheckSkip(f"docker 调用失败: {exc}") from exc
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-300:]
        raise CheckSkip(f"镜像 {image} 拉取失败: {tail}")


def docker_run(args: list[str], timeout: int = DOCKER_RUN_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", *args],
                          capture_output=True, text=True, timeout=timeout)


def check_docker_promtool(docs: dict) -> str:
    docker_ready()
    svc = compose_services(docs)["prometheus"]
    image = str(svc["image"])
    ensure_image(image)
    mounts = bind_mounts(svc)
    args: list[str] = []
    for host, container in mounts:
        if container.startswith("/etc/prometheus") and host.is_file():
            args += ["-v", f"{host.resolve().as_posix()}:{container}:ro"]
    # prom/prometheus 镜像的 entrypoint 是 prometheus 本体，跑 promtool 需显式覆盖
    args += ["--entrypoint", "promtool", image, "check", "config", PROM_CONTAINER_PATH]
    try:
        r = docker_run(args)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"promtool 运行超时（镜像已在本地，非拉取问题）: {exc}") from exc
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise CheckError(f"promtool check config 失败:\n{out.strip()[-800:]}")
    ok_lines = [ln.strip() for ln in out.splitlines() if "SUCCESS" in ln or "FAILED" in ln]
    return "; ".join(ok_lines) if ok_lines else "promtool 退出码 0"


def check_docker_amtool(docs: dict) -> str:
    docker_ready()
    svc = compose_services(docs)["alertmanager"]
    image = str(svc["image"])
    ensure_image(image)
    if not AM_YML.is_file():
        raise CheckError(f"{rel(AM_YML)} 不存在")
    args = ["-v", f"{AM_YML.resolve().as_posix()}:/tmp/alertmanager.yml:ro",
            "--entrypoint", "amtool", image, "check-config", "/tmp/alertmanager.yml"]
    try:
        r = docker_run(args)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"amtool 运行超时（镜像已在本地，非拉取问题）: {exc}") from exc
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise CheckError(f"amtool check-config 失败:\n{out.strip()[-800:]}")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    verdict = next((ln for ln in lines if "SUCCESS" in ln or "valid" in ln.lower()),
                   lines[-1] if lines else "")
    return verdict or "amtool 退出码 0"


def check_docker_loki_verify(docs: dict) -> str:
    docker_ready()
    svc = compose_services(docs)["loki"]
    image = str(svc["image"])
    ensure_image(image)
    cmd = command_str(svc)
    target = cmd.split("config.file=", 1)[1].split()[0].strip() if "config.file=" in cmd \
        else "/etc/loki/config.yaml"
    args = ["-v", f"{LOKI_YML.resolve().as_posix()}:{target}:ro",
            "--entrypoint", "loki", image,
            f"-config.file={target}", "-verify-config"]
    try:
        r = docker_run(args)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"loki -verify-config 运行超时（镜像已在本地）: {exc}") from exc
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise CheckError(f"loki -verify-config 失败:\n{out.strip()[-800:]}")
    return "loki -verify-config 通过（配置可被 Loki 3.2 运行时接受）"


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="EAP 观测配置一致性检查（M49-D）")
    ap.add_argument("--docker", action="store_true",
                    help="用容器内 promtool/amtool/loki 做深度校验（镜像拉不动则 SKIP）")
    args = ap.parse_args(argv)

    files, docs, problems = load_all_yaml()
    if problems:
        record(FAIL, "yaml-parse", "; ".join(problems))
    else:
        record(PASS, "yaml-parse", f"{len(files)} 个 YAML 全部解析 OK（{', '.join(rel(f) for f in files)}）")

    run_check("prom-rule-files", lambda: check_prometheus_rule_files(docs))
    run_check("prom-alerting", lambda: check_prometheus_alerting(docs))
    run_check("compose-prometheus", lambda: check_compose_prometheus_service(docs))
    run_check("loki-retention", lambda: check_loki_config(docs))
    run_check("promtail-trace-id", lambda: check_promtail_trace_id(docs))
    run_check("am-route-receivers", lambda: check_alertmanager_routes(docs))
    run_check("alert-rules", lambda: check_alert_rules(docs))

    if args.docker:
        run_check("docker-promtool", lambda: check_docker_promtool(docs))
        run_check("docker-amtool", lambda: check_docker_amtool(docs))
        run_check("docker-loki-verify", lambda: check_docker_loki_verify(docs))

    width = max(len(name) for _, name, _ in RESULTS)
    n_pass = n_fail = n_skip = 0
    for status, name, detail in RESULTS:
        n_pass += status == PASS
        n_fail += status == FAIL
        n_skip += status == SKIP
        print(f"[{status}] {name.ljust(width)} : {detail}")
    print(f"\ncheck_observability: {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP"
          + ("（--docker）" if args.docker else ""))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

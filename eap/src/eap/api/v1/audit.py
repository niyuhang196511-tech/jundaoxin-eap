"""审计日志（M11 / v0.6 增强 / M47-B 治理）：管理面操作追踪（过滤 + 分页）+ 导出 + 保留期清理
+ Harness 审计上报（M41-B）。

查询端点：admin 语义（action 精确 / actor 精确 / target 前缀 / 时间范围 + limit/offset 分页）；
导出端点 GET /export：admin 专用流式 CSV/JSON（行数上限 EAP_AUDIT_EXPORT_LIMIT 防拖库），
导出动作自身落审计 audit.export（仅条数与过滤条件）；
保留期清理 POST /purge：admin 显式触发（对齐 memory /purge 模式，无启动/周期自动扫描），
删除超过 EAP_AUDIT_RETENTION_DAYS 的行，动作落审计 audit.retention.purge（仅条数）；
上报端点 POST /harness-report：任意有效凭证（resolve_tenant）但仅设备 Key 可写——
从 Authorization 反查 ApiKey note（harness:<name>），条目 action 强制 harness. 前缀防伪造。
"""

from __future__ import annotations

import json
from datetime import datetime  # 模块级：_parse_local_created_at 返回注解具名（ruff F821）

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import SessionLocal, get_db
from ...models import ApiKey, AuditLog
from ..deps import require_admin, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/audit", dependencies=[fastapi.Depends(resolve_tenant)])


def _filtered_query(action: str | None, actor: str | None, target: str | None,
                    since: str | None, until: str | None):
    """查询/导出共用的过滤语义（v0.6-M23）：
    action 精确 / actor 精确 / target 前缀 / 时间范围（ISO 日期或日期时间）。"""
    from datetime import datetime, timezone

    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(AuditLog.actor == actor)
    if target:
        query = query.where(AuditLog.target.startswith(target))
    for field, raw, direction in (("since", since, 1), ("until", until, -1)):
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400,
                                        detail=f"EAP-4000 {field} 须为 ISO 日期/日期时间: {raw}") from e
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if direction == -1:  # until 含当天/当秒：+1 天（仅日期时）由调用方语义决定，这里精确到给定时刻
            query = query.where(AuditLog.created_at <= moment.replace(tzinfo=None))
        else:
            query = query.where(AuditLog.created_at >= moment.replace(tzinfo=None))
    return query


@router.get("", dependencies=[fastapi.Depends(require_admin)])
def list_audit(
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = fastapi.Depends(get_db),
):
    """审计日志查询：action 精确 / actor 精确 / target 前缀 / 时间范围（ISO 日期或日期时间）。"""
    query = _filtered_query(action, actor, target, since, until)
    query = (query.order_by(AuditLog.created_at.desc())
             .limit(max(1, min(limit, 500)))
             .offset(max(0, offset)))
    return [
        {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target,
         "detail": a.detail, "trace_id": a.trace_id, "created_at": str(a.created_at)}
        for a in db.scalars(query).all()
    ]


@router.get("/actions", dependencies=[fastapi.Depends(require_admin)])
def list_audit_actions():
    """审计动作目录（M50-B2）：供审计查询页动作过滤做 datalist 候选提示。

    目录为文档性质（observability/audit_actions.AUDIT_ACTIONS，与全仓留痕调用点
    静态核实同步，防漂移测试 tests/test_audit_actions.py）；查询端点的 action
    过滤仍接受任意字符串（精确匹配），目录不构成白名单校验。
    Harness 上报等动态动作名（harness.<客户端提供>）不在目录内属预期。
    """
    from ...observability.audit_actions import AUDIT_ACTIONS

    return list(AUDIT_ACTIONS)


# ---------- 审计导出（M47-B）：admin 流式 CSV/JSON，行数上限防拖库 ----------

EXPORT_COLUMNS = ["id", "created_at", "action", "actor", "target", "trace_id", "detail"]
EXPORT_CHUNK_SIZE = 500  # 流式分块：每批从库取的行数（导出全程内存有界，不做全量物化）
EXPORT_DEFAULT_LIMIT = 50000  # EAP_AUDIT_EXPORT_LIMIT 配置非法（<=0）时回落的上限


def _export_limit(requested: int | None) -> int:
    """导出行数上限（防拖库）：EAP_AUDIT_EXPORT_LIMIT（默认 50000）为硬上限且**不可关闭**——
    配置 0/负值视为非法回落默认；请求显式 limit 仅允许在硬上限内取更小（如导最近 N 条）。"""
    configured = get_settings().audit_export_limit
    cap = configured if configured > 0 else EXPORT_DEFAULT_LIMIT
    if requested is None or requested <= 0:
        return cap
    return min(requested, cap)


def _export_stream(fmt: str, base_query, cap: int, filters: dict, actor: str, trace_id: str):
    """流式导出生成器：分块查询（每批 EXPORT_CHUNK_SIZE 条）逐块 yield，
    全程只持有一批 ORM 行；结束时（含客户端中断）落审计 audit.export——仅条数与过滤条件。"""
    import csv as csv_mod
    from io import StringIO

    from ...observability import audit

    count = 0
    first = True
    try:
        with SessionLocal() as db:  # 独立会话：不依赖请求级事务的生命周期
            if fmt == "csv":
                buf = StringIO()
                csv_mod.writer(buf).writerow(EXPORT_COLUMNS)
                yield "\ufeff" + buf.getvalue()  # UTF-8 BOM：Excel 直开中文不乱码
            else:
                yield "["
            offset = 0
            while count < cap:
                size = min(EXPORT_CHUNK_SIZE, cap - count)
                chunk = db.scalars(base_query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                                   .offset(offset).limit(size)).all()
                if not chunk:
                    break
                for a in chunk:
                    if fmt == "csv":
                        buf = StringIO()
                        csv_mod.writer(buf).writerow([
                            a.id, str(a.created_at), a.action, a.actor, a.target, a.trace_id,
                            json.dumps(a.detail or {}, ensure_ascii=False)])
                        yield buf.getvalue()
                    else:
                        row = {"id": a.id, "created_at": str(a.created_at), "action": a.action,
                               "actor": a.actor, "target": a.target, "trace_id": a.trace_id,
                               "detail": a.detail if isinstance(a.detail, dict) else {}}
                        yield ("" if first else ",") + json.dumps(row, ensure_ascii=False, default=str)
                    first = False
                    count += 1
                if len(chunk) < size:
                    break
                offset += size
            if fmt != "csv":
                yield "]"
    finally:
        audit.record("audit.export", actor=actor, target="*",
                     detail={**filters, "format": fmt, "count": count, "limit": cap},
                     trace_id=trace_id)


@router.get("/export", dependencies=[fastapi.Depends(require_admin)])
def export_audit(
    format: str = "csv",
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    request: fastapi.Request = None,
):
    """审计日志导出（M47-B）：admin 专用，过滤语义与查询端点完全一致。

    - format=csv（默认，UTF-8 BOM，列 id/created_at/action/actor/target/trace_id/detail(JSON 字符串)）
      | json（对象数组）；
    - StreamingResponse 分块流式响应，避免大结果集全量进内存；
    - 行数上限 EAP_AUDIT_EXPORT_LIMIT（默认 50000）：硬上限不可关闭（防拖库），
      请求可传更小的 limit；Content-Disposition 为附件下载，文件名含 UTC 时间戳；
    - 导出动作本身落审计 audit.export（仅条数 + 过滤条件 + format，不含导出内容）。
    """
    from datetime import datetime, timezone

    from ...observability import audit

    fmt = (format or "csv").lower()
    if fmt not in ("csv", "json"):
        raise fastapi.HTTPException(status_code=422, detail="EAP-4000 format 须为 csv 或 json")
    filters = {"action": action or "", "actor": actor or "", "target": target or "",
               "since": since or "", "until": until or ""}
    cap = _export_limit(limit)
    query = _filtered_query(action, actor, target, since, until)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return fastapi.responses.StreamingResponse(
        _export_stream(fmt, query, cap, filters, audit.actor_of(request),
                       getattr(request.state, "trace_id", "")),
        media_type="text/csv; charset=utf-8" if fmt == "csv" else "application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-export-{stamp}.{fmt}"'},
    )


# ---------- 审计保留期清理（M47-B）：admin 显式触发，对齐 memory /purge 模式 ----------


@router.post("/purge", dependencies=[fastapi.Depends(require_admin)])
def purge_retention(request: fastapi.Request, retention_days: int | None = None,
                    db: Session = fastapi.Depends(get_db)):
    """审计保留期清理：删除 created_at 早于 EAP_AUDIT_RETENTION_DAYS（默认 365）的行。

    挂点对齐 memory retention（EAP_MEMORY_RETENTION_DAYS + POST /memory/purge）：
    admin 显式触发，无启动/周期自动扫描；显式传 retention_days 可单次覆盖（运维一次性收紧）；
    0=永久保留（不删）。动作本身落审计 audit.retention.purge（仅条数，不含内容）。
    """
    from ...observability import audit

    retention = get_settings().audit_retention_days if retention_days is None else retention_days
    count = audit.purge_expired(db, retention)
    audit.record("audit.retention.purge", actor=audit.actor_of(request), target="*",
                 detail={"retention_days": retention, "deleted": count},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"deleted": count, "retention_days": retention}


# ---------- Harness 审计上报（M41-B，docs/18 M40 后置项）：企业可选、显式开启 ----------

HARNESS_REPORT_MAX_ENTRIES = 500  # 单批上限（防大 payload；客户端分批）


def _bearer_token(request: fastapi.Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _harness_device_of(request: fastapi.Request, db: Session) -> tuple[str, str | None] | None:
    """从请求 Authorization 反查 Harness 设备 Key（resolve_tenant 不落 key note，故此处反查）。

    匹配顺序与 deps.resolve_tenant 一致：key_hash 优先、明文列兼容回退。
    返回 (设备名, device_user)；非设备 Key（note 非 harness: 前缀）返回 None。
    """
    token = _bearer_token(request)
    if not token:
        return None
    from ...security_keys import key_hash

    record = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash(token),
                                            ApiKey.enabled == True))  # noqa: E712
    if record is None:
        record = db.scalar(select(ApiKey).where(ApiKey.key == token,
                                                ApiKey.enabled == True))  # noqa: E712
    if record is None or not record.note.startswith("harness:"):
        return None
    return record.note.removeprefix("harness:"), record.device_user


def _parse_local_created_at(raw) -> "datetime | None":
    """Harness 本地 created_at：epoch 秒（字符串/数字）或 ISO 文本；不可解析返回 None（用当前时刻）。"""
    from datetime import datetime, timezone

    if raw is None or raw == "":
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return moment.replace(tzinfo=None) if moment.tzinfo is None else \
            moment.astimezone(timezone.utc).replace(tzinfo=None)
    if not 0 < seconds < 4102444800:  # 1970~2100 界外视为垃圾数据
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


@router.post("/harness-report")
def harness_report(body: dict, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """Harness 本地技能执行审计批量上报（M41-B）。

    授权：任意有效凭证可过 resolve_tenant，但**仅设备 Key**（note=harness:<name>）可写——
    非设备凭证无从归属设备，一律 403。条目逐条落平台 AuditLog：

    - action 强制 ``harness.`` 前缀（缺失则改写补前缀，已有前缀原样）——防伪造他类审计；
    - actor = ``harness:<device>:<device_user 或 ?>``（设备-用户配对，M40-A）；
    - target = 设备名；created_at 取本地发生时刻（epoch 秒或 ISO），缺失用当前时刻；
    - 上报动作本身落 ``harness.audit.report`` 审计（仅条数，不含内容）。
    """
    from datetime import datetime, timezone

    from ...observability import audit

    device = _harness_device_of(request, db)
    if device is None:
        raise fastapi.HTTPException(status_code=403,
                                    detail="EAP-3003 审计上报仅限 Harness 设备 Key")
    name, device_user = device

    entries = (body or {}).get("entries")
    if not isinstance(entries, list):
        raise fastapi.HTTPException(status_code=422, detail="EAP-4000 entries 须为数组")
    if len(entries) > HARNESS_REPORT_MAX_ENTRIES:
        raise fastapi.HTTPException(
            status_code=422, detail=f"EAP-4000 单批最多 {HARNESS_REPORT_MAX_ENTRIES} 条")

    actor = f"harness:{name}:{device_user or '?'}"
    accepted = 0
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        if not action:
            continue
        if not action.startswith("harness."):  # 前缀强制：改写而非拒绝（单批内坏条目不拖垮整批）
            action = f"harness.{action}"
        moment = _parse_local_created_at(raw.get("created_at")) or \
            datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(AuditLog(action=action[:64], actor=actor[:128], target=name[:128],
                        detail={"detail": str(raw.get("detail") or "")[:512]},
                        created_at=moment))
        accepted += 1
    if accepted:
        db.flush()
    audit.record("harness.audit.report", actor=actor, target=name,
                 detail={"count": accepted}, trace_id=getattr(request.state, "trace_id", ""),
                 db=db)
    db.commit()
    return {"accepted": accepted}

"""审计日志（M11 / v0.6 增强）：管理面操作追踪（过滤 + 分页）+ Harness 审计上报（M41-B）。

查询端点：admin 语义（action 精确 / actor 精确 / target 前缀 / 时间范围 + limit/offset 分页）；
上报端点 POST /harness-report：任意有效凭证（resolve_tenant）但仅设备 Key 可写——
从 Authorization 反查 ApiKey note（harness:<name>），条目 action 强制 harness. 前缀防伪造。
"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import ApiKey, AuditLog
from ..deps import require_admin, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/audit", dependencies=[fastapi.Depends(resolve_tenant)])


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
    from datetime import datetime, timedelta, timezone

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
    query = (query.order_by(AuditLog.created_at.desc())
             .limit(max(1, min(limit, 500)))
             .offset(max(0, offset)))
    return [
        {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target,
         "detail": a.detail, "trace_id": a.trace_id, "created_at": str(a.created_at)}
        for a in db.scalars(query).all()
    ]


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

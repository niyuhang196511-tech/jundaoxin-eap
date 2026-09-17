"""模型中心 API：注册表查询 + 定制/专用模型注册（docs/04 §2.3 的 M1 版）。"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import ModelRecord
from ...schemas import ModelRegister
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/models",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


@router.get("")
def list_models(db: Session = fastapi.Depends(get_db)):
    return [
        {
            "name": m.name, "capabilities": m.capabilities, "provider": m.provider,
            "priority": m.priority, "enabled": m.enabled, "notes": m.notes,
            "endpoint": m.base_url,
        }
        for m in db.scalars(select(ModelRecord)).all()
    ]


@router.post("")
def register_model(body: ModelRegister, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """注册定制/专用模型：M1 直接上架（enabled=True）；评测门禁→灰度→上架流水线在 M2（docs/06 §4）。"""
    if db.scalar(select(ModelRecord).where(ModelRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 模型 {body.name} 已注册")
    if body.provider == "openai_compat" and not (body.base_url and body.api_key):
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 openai_compat 需要 base_url 与 api_key")
    from ...observability import audit as _audit
    from ...security_crypto import encrypt_secret

    _audit.record("model.register", actor=_audit.actor_of(request), target=body.name,
                  detail={"provider": body.provider, "capabilities": body.capabilities,
                          "priority": body.priority}, trace_id=getattr(request.state, "trace_id", ""),
                  db=db)
    model_record = ModelRecord(
        name=body.name, capabilities=body.capabilities, provider=body.provider,
        base_url=body.base_url, api_key=encrypt_secret(body.api_key),
        remote_model=body.remote_model,
        priority=body.priority, notes=body.notes,
    )
    db.add(model_record)
    db.commit()
    return {"name": model_record.name, "capabilities": model_record.capabilities, "provider": model_record.provider,
            "priority": model_record.priority, "enabled": model_record.enabled}


@router.patch("/{name}")
def toggle_model(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(ModelRecord).where(ModelRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 模型 {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": name, "enabled": enabled}

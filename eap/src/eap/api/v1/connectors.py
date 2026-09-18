"""企业连接器 API（docs/04 §4，M3）：登记 / 验证 / 启停 / 工具清单。"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import ConnectorRecord
from ...runtime.connectors import load_connector_tools
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/connectors",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class EndpointDef(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,40}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,60}$",
                           description="平台工具全名，如 erp.order.create")
    method: str = Field(default="GET", pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")
    path: str = Field(default="/", max_length=200)
    description: str = ""
    requires_approval: bool = False
    params: dict | None = None


class ConnectorCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    kind: str = Field(default="rest", pattern=r"^(rest|mock-erp)$")
    description: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=256)
    header_name: str = "Authorization"
    api_key: str | None = None
    endpoints: list[EndpointDef] = Field(min_length=1, max_length=32)
    enabled: bool = True


def _view(r: ConnectorRecord) -> dict:
    return {"name": r.name, "kind": r.kind, "description": r.description,
            "base_url": r.base_url, "status": r.status, "enabled": r.enabled,
            "endpoints": [e.get("tool_name") for e in (r.endpoints or [])],
            "created_at": str(r.created_at)}


@router.get("")
def list_connectors(db: Session = fastapi.Depends(get_db)):
    return [_view(r) for r in db.scalars(select(ConnectorRecord)).all()]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def create_connector(body: ConnectorCreate, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 连接器 {body.name} 已注册")
    if body.kind == "rest" and not body.base_url.lower().startswith(("http://", "https://")):
        raise fastapi.HTTPException(status_code=400, detail="EAP-7002 rest 连接器必须提供 http(s) base_url")
    from ...security_crypto import encrypt_secret

    record = ConnectorRecord(
        name=body.name, kind=body.kind, description=body.description,
        base_url=body.base_url, header_name=body.header_name,
        api_key=encrypt_secret(body.api_key),
        endpoints=[e.model_dump() for e in body.endpoints], enabled=body.enabled,
    )
    db.add(record)
    db.commit()
    return _view(record)


@router.post("/{name}/validate")
async def validate_connector(name: str, db: Session = fastapi.Depends(get_db)):
    """连通性验证：mock-erp 恒定可达；rest 对 base_url 发一次 HEAD/GET 探测。"""
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    if record.kind == "mock-erp":
        record.status = "verified"
        db.commit()
        return {"name": record.name, "status": record.status, "detail": "内置演示 ERP，恒定可达"}

    import httpx

    try:
        from ...runtime.connectors import _safe_url

        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            headers = {}
            if record.api_key:
                from ...security_crypto import decrypt_secret

                headers[record.header_name or "Authorization"] = decrypt_secret(record.api_key)
            resp = await client.get(_safe_url(record.base_url, "/"), headers=headers)
        record.status = "verified" if resp.status_code < 500 else "unreachable"
        detail = f"HTTP {resp.status_code}"
    except Exception as e:  # 网络/超时/DNS
        record.status = "unreachable"
        detail = str(e)[:200]
    db.commit()
    return {"name": record.name, "status": record.status, "detail": detail}


@router.post("/{name}/enabled", dependencies=[fastapi.Depends(require_admin)])
def toggle(name: str, enabled: bool, db: Session = fastapi.Depends(get_db)):
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    record.enabled = enabled
    db.commit()
    return {"name": record.name, "enabled": record.enabled}


@router.get("/{name}/tools")
def connector_tools(name: str, db: Session = fastapi.Depends(get_db)):
    """该连接器注入平台工具池的 OpenAI function-calling Schema。"""
    record = db.scalar(select(ConnectorRecord).where(ConnectorRecord.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 连接器 {name} 不存在")
    return [t.schema() for t in load_connector_tools(record)]

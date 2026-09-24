"""租户目录 API（M50-B1）：admin 专用的租户列表，控制台租户下拉的数据源。

此前前端（Governance 成本预算）只能手输租户 ID；本端点把 Tenant 表实际字段
（id/name/created_at，见 models/__init__.py）透出为下拉选项。RBAC 对齐 audit.py：
路由级 resolve_tenant（三轨凭证）+ 端点级 require_admin（API Key / admin JWT，
member 与会话令牌 403）——非 admin 前端降级回手输。
"""

from __future__ import annotations

import fastapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import Tenant
from ..deps import require_admin, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/tenants",
                           dependencies=[fastapi.Depends(resolve_tenant)])


@router.get("", dependencies=[fastapi.Depends(require_admin)])
def list_tenants(db: Session = fastapi.Depends(get_db)):
    """租户列表（admin）：id 升序，字段与 Tenant 表一一对应（不虚构）。"""
    return [{"id": t.id, "name": t.name, "created_at": str(t.created_at)}
            for t in db.scalars(select(Tenant).order_by(Tenant.id)).all()]

"""LoRA adapter 托管 API（M42-A，L1 工程部分；docs/10 遗留项 / docs/20 部署指南）。

注册表 CRUD（admin+审计 lora.*）+ load/unload（调 VllmProvider 走 vLLM 管理端点）+
health 透出。vLLM 地址解析：入参显式 base_url > 模型中心内指向该 adapter 的
ModelRecord（provider=vllm 且模型名匹配 served 名）。HTTP 出站经可注入 provider
（eap.modelhub.vllm.get_vllm_provider，测试替换为假件，零真实网络）。
"""

from __future__ import annotations

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...modelhub.providers import ProviderError
from ...modelhub.vllm import get_vllm_provider
from ...models import LoraAdapter, ModelRecord
from ...observability import audit
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/lora",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class LoraRegister(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,60}$",
                      description="adapter 名 = vLLM 上的模型名")
    base_model: str = Field(max_length=128, description="基座模型（vLLM serve 的 model）")
    source_path: str = Field(max_length=512,
                             description="GPU 宿主机（WSL2/容器内）上的 adapter 目录")
    served_as: str | None = Field(default=None, max_length=64,
                                  description="vLLM 服务名，默认=name")
    note: str = Field(default="", max_length=256)


class LoraAction(BaseModel):
    base_url: str | None = Field(default=None, max_length=256,
                                 description="显式 vLLM 地址；缺省从模型中心解析")


def _view(r: LoraAdapter) -> dict:
    return {"name": r.name, "base_model": r.base_model, "source_path": r.source_path,
            "served_as": r.served_as or r.name, "status": r.status, "note": r.note,
            "created_at": str(r.created_at), "updated_at": str(r.updated_at)}


def _get_adapter(db: Session, name: str) -> LoraAdapter:
    record = db.scalar(select(LoraAdapter).where(LoraAdapter.name == name))
    if record is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 LoRA adapter {name} 不存在")
    return record


def _resolve_base_url(db: Session, adapter: LoraAdapter, explicit: str | None) -> str:
    """vLLM 地址：显式传入 > 模型中心 provider=vllm 且模型名匹配该 adapter 的记录。"""
    if explicit:
        return explicit
    served = adapter.served_as or adapter.name
    record = db.scalar(
        select(ModelRecord).where(ModelRecord.provider == "vllm").where(
            or_(ModelRecord.remote_model == served, ModelRecord.name == served)))
    if record is not None and record.base_url:
        return record.base_url
    raise fastapi.HTTPException(
        status_code=400,
        detail="EAP-4000 无法确定 vLLM 地址：入参显式传 base_url，"
               "或先在模型中心注册 provider=vllm 且模型名匹配的记录")


@router.get("")
def list_adapters(db: Session = fastapi.Depends(get_db)):
    return [_view(r) for r in db.scalars(select(LoraAdapter)).all()]


@router.post("", dependencies=[fastapi.Depends(require_admin)])
def register_adapter(body: LoraRegister, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if db.scalar(select(LoraAdapter).where(LoraAdapter.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 LoRA adapter {body.name} 已注册")
    record = LoraAdapter(name=body.name, base_model=body.base_model, source_path=body.source_path,
                         served_as=body.served_as or body.name, status="registered", note=body.note)
    db.add(record)
    audit.record("lora.register", actor=audit.actor_of(request), target=body.name,
                 detail={"base_model": body.base_model, "served_as": record.served_as},
                 trace_id=getattr(request.state, "trace_id", ""), db=db)  # 随同一事务提交
    db.commit()
    return _view(record)


@router.delete("/{name}", dependencies=[fastapi.Depends(require_admin)])
def delete_adapter(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    record = _get_adapter(db, name)
    if record.status == "loaded":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-2003 adapter {name} 仍在 vLLM 上加载，请先 unload")
    db.delete(record)
    audit.record("lora.delete", actor=audit.actor_of(request), target=name,
                 detail={}, trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    return {"name": name, "deleted": True}


@router.post("/{name}/load", dependencies=[fastapi.Depends(require_admin)])
async def load_adapter(name: str, request: fastapi.Request,
                       body: LoraAction | None = None,
                       db: Session = fastapi.Depends(get_db)):
    """动态加载到 vLLM（POST /v1/load_lora_adapter）：成功 → loaded；失败 → failed + note 记错误。"""
    record = _get_adapter(db, name)
    base_url = _resolve_base_url(db, record, (body.base_url if body else None))
    try:
        result = await get_vllm_provider().load_lora(base_url, record.name, record.source_path)
    except ProviderError as e:
        record.status, record.note = "failed", str(e)[:500]
        audit.record("lora.load", actor=audit.actor_of(request), target=name,
                     detail={"base_url": base_url, "ok": False, "error": str(e)[:200]},
                     trace_id=getattr(request.state, "trace_id", ""), db=db)
        db.commit()
        raise fastapi.HTTPException(status_code=502, detail=f"EAP-7004 vLLM 加载失败: {e}") from e
    record.status, record.note = "loaded", ""
    audit.record("lora.load", actor=audit.actor_of(request), target=name,
                 detail={"base_url": base_url, "ok": True},
                 trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    audit.record("lora.load", actor=audit.actor_of(request), target=name,
                 detail={"base_url": base_url, "ok": True},
                 trace_id=getattr(request.state, "trace_id", ""), db=db)
    view = _view(record)
    view["vllm"] = result
    return view


@router.post("/{name}/unload", dependencies=[fastapi.Depends(require_admin)])
async def unload_adapter(name: str, request: fastapi.Request,
                         body: LoraAction | None = None,
                         db: Session = fastapi.Depends(get_db)):
    """从 vLLM 卸载（POST /v1/unload_lora_adapter）：成功 → unloaded；失败 → failed + note 记错误。"""
    record = _get_adapter(db, name)
    base_url = _resolve_base_url(db, record, (body.base_url if body else None))
    try:
        result = await get_vllm_provider().unload_lora(base_url, record.name)
    except ProviderError as e:
        record.status, record.note = "failed", str(e)[:500]
        audit.record("lora.unload", actor=audit.actor_of(request), target=name,
                     detail={"base_url": base_url, "ok": False, "error": str(e)[:200]},
                     trace_id=getattr(request.state, "trace_id", ""), db=db)
        db.commit()
        raise fastapi.HTTPException(status_code=502, detail=f"EAP-7004 vLLM 卸载失败: {e}") from e
    record.status, record.note = "unloaded", ""
    audit.record("lora.unload", actor=audit.actor_of(request), target=name,
                 detail={"base_url": base_url, "ok": True},
                 trace_id=getattr(request.state, "trace_id", ""), db=db)
    db.commit()
    audit.record("lora.unload", actor=audit.actor_of(request), target=name,
                 detail={"base_url": base_url, "ok": True},
                 trace_id=getattr(request.state, "trace_id", ""), db=db)
    view = _view(record)
    view["vllm"] = result
    return view


@router.get("/{name}/health")
async def adapter_health(name: str, base_url: str | None = fastapi.Query(default=None),
                         db: Session = fastapi.Depends(get_db)):
    """vLLM 健康检查透出：healthy + 当前服务模型清单 + 本 adapter 是否在列。"""
    record = _get_adapter(db, name)
    resolved = _resolve_base_url(db, record, base_url)
    health = await get_vllm_provider().health(resolved)
    served = (record.served_as or record.name)
    return {"name": record.name, "base_url": resolved,
            "healthy": health.get("healthy", False),
            "models": health.get("models", []),
            "served": served in health.get("models", []),
            **({"error": health["error"]} if health.get("error") else {})}

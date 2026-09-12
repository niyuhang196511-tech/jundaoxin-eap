"""评测中心 API：数据集 + 评测运行 + 门禁结论（docs/08 §3 的规则裁判子集）。

LLM-as-Judge / 人工抽检 / 在线影子流量在 M2-M3 接入。
"""

from __future__ import annotations

import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import EvalDatasetRecord, EvalRunRecord
from ...schemas import InvokeRequest
from ..deps import require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/evals",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class EvalDatasetCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    description: str = ""
    cases: list[dict] = Field(min_length=1,
                              description='[{"input": "...", "expected_any": ["关键词1","关键词2"]}]')


class EvalRunRequest(BaseModel):
    agent: str
    dataset: str
    min_pass_rate: float = Field(default=0.8, ge=0.0, le=1.0)


@router.post("/datasets")
def create_dataset(body: EvalDatasetCreate, db: Session = fastapi.Depends(get_db)):
    for i, case in enumerate(body.cases):
        if "input" not in case or "expected_any" not in case:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"EAP-4000 用例 {i} 缺少 input/expected_any 字段")
    if db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 数据集 {body.name} 已存在")
    record = EvalDatasetRecord(name=body.name, description=body.description, cases=body.cases)
    db.add(record)
    db.commit()
    return {"name": record.name, "cases": len(record.cases)}


@router.get("/datasets")
def list_datasets(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": d.name, "description": d.description, "cases": len(d.cases)}
        for d in db.scalars(select(EvalDatasetRecord)).all()
    ]


@router.post("/runs")
async def run_evaluation(body: EvalRunRequest, db: Session = fastapi.Depends(get_db)):
    result = await execute_evaluation(db, body.agent, body.dataset, body.min_pass_rate)
    return result


async def execute_evaluation(db: Session, agent: str, dataset_name: str,
                             min_pass_rate: float = 0.8) -> dict:
    """规则裁判评测（发布门禁复用）：输出包含任一关键词即通过；通过率过门禁 → PASS。"""
    dataset = db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == dataset_name))
    if dataset is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 数据集 {dataset_name} 不存在")

    scores = []
    passed = 0
    for case in dataset.cases:
        try:
            resp = await _invoke(db, agent, case["input"])
            output = resp["output"]
        except Exception as e:
            scores.append({"input": case["input"], "passed": False, "error": str(e)[:200]})
            continue
        ok = any(kw in output for kw in case.get("expected_any", []))
        passed += 1 if ok else 0
        scores.append({"input": case["input"], "passed": ok, "output_snippet": output[:120]})

    rate = passed / len(dataset.cases) if dataset.cases else 0.0
    verdict = "PASS" if rate >= min_pass_rate else "FAIL"
    run = EvalRunRecord(id=uuid.uuid4().hex, agent=agent, dataset=dataset_name,
                        verdict=verdict, min_pass_rate=min_pass_rate, scores=scores)
    db.add(run)
    db.commit()
    return {"run_id": run.id, "agent": agent, "dataset": dataset_name,
            "verdict": verdict, "pass_rate": round(rate, 4), "scores": scores}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = fastapi.Depends(get_db)):
    run = db.get(EvalRunRecord, run_id)
    if run is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 评测运行 {run_id} 不存在")
    return {"run_id": run.id, "agent": run.agent, "dataset": run.dataset,
            "verdict": run.verdict, "pass_rate": None, "scores": run.scores}


async def _invoke(db: Session, agent: str, text: str) -> dict:
    from ...agents.registry import registry

    resp = await registry.invoke(db, agent, InvokeRequest(input=text))
    return {"output": resp.output}

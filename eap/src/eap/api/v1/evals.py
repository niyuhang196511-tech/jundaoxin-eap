"""评测中心 API：数据集 + 评测运行 + 门禁结论（docs/08 §3 的规则裁判子集）。

LLM-as-Judge / 人工抽检 / 在线影子流量在 M2-M3 接入。
"""

from __future__ import annotations

import json
import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import EvalDatasetRecord, EvalRunRecord
from ...schemas import InvokeRequest
from ..deps import require_admin, require_api_key, resolve_tenant

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
    judge: str = Field(default="rule", pattern=r"^(rule|llm)$",
                       description="rule=关键词命中；llm=LLM-as-Judge 按评分标准裁判")


@router.post("/datasets", dependencies=[fastapi.Depends(require_admin)])
def create_dataset(body: EvalDatasetCreate, db: Session = fastapi.Depends(get_db)):
    for i, case in enumerate(body.cases):
        if "input" not in case or not ("expected_any" in case or "expectation" in case):
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"EAP-4000 用例 {i} 缺少 input/expected_any（或 llm 裁判用的 expectation）字段")
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


@router.post("/runs", dependencies=[fastapi.Depends(require_admin)])
async def run_evaluation(body: EvalRunRequest, db: Session = fastapi.Depends(get_db)):
    return await execute_evaluation(db, body.agent, body.dataset, body.min_pass_rate, body.judge)


async def execute_evaluation(db: Session, agent: str, dataset_name: str,
                             min_pass_rate: float = 0.8, judge: str = "rule") -> dict:
    """评测执行（发布门禁复用）：rule=关键词命中；llm=LLM-as-Judge（docs/08 §3）。"""
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
        if judge == "llm":
            verdict = await _llm_judge(db, case["input"], output,
                                       case.get("expectation") or _keywords_criteria(case))
            ok = verdict["passed"]
            scores.append({"input": case["input"], "passed": ok, "judge": verdict,
                           "output_snippet": output[:120]})
        else:
            ok = any(kw in output for kw in case.get("expected_any", []))
            scores.append({"input": case["input"], "passed": ok, "output_snippet": output[:120]})
        passed += 1 if ok else 0

    rate = passed / len(dataset.cases) if dataset.cases else 0.0
    verdict = "PASS" if rate >= min_pass_rate else "FAIL"
    run = EvalRunRecord(id=uuid.uuid4().hex, agent=agent, dataset=dataset_name,
                        verdict=verdict, min_pass_rate=min_pass_rate, judge=judge, scores=scores)
    db.add(run)
    db.commit()
    return {"run_id": run.id, "agent": agent, "dataset": dataset_name, "judge": judge,
            "verdict": verdict, "pass_rate": round(rate, 4), "scores": scores}


def _keywords_criteria(case: dict) -> str:
    kws = "、".join(case.get("expected_any", []))
    return f"回答需体现以下关键词或其语义：{kws}" if kws else "回答应切题且符合客服规范"


async def _llm_judge(db: Session, case_input: str, output: str, expectation: str) -> dict:
    """LLM-as-Judge：模型输出 JSON {"passed": bool, "reason": str}；解析失败判不通过。"""
    from ...modelhub.router import hub

    system = ("EAP-JUDGE 你是严格的评测裁判。依据评分标准判断智能体输出是否合格。"
              '只输出一个 JSON 对象，禁止输出其他任何文字：{"passed": true, "reason": "简短理由"}')
    user = (f"【评分标准】\n{expectation}\n\n【用户输入】\n{case_input}\n\n"
            f"【智能体输出】\n{output[:2000]}")
    try:
        completion = await hub.complete(
            db, [{"role": "system", "content": system}, {"role": "user", "content": user}],
            capability="chat")
        raw = completion.result.content or ""
    except Exception as e:
        return {"passed": False, "reason": f"裁判调用失败: {str(e)[:120]}"}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"passed": False, "reason": "裁判输出无法解析为 JSON", "raw": raw[:160]}
    try:
        parsed = json.loads(raw[start:end + 1])
        return {"passed": bool(parsed.get("passed")), "reason": str(parsed.get("reason", ""))[:200]}
    except json.JSONDecodeError:
        return {"passed": False, "reason": "裁判输出无法解析为 JSON", "raw": raw[:160]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = fastapi.Depends(get_db)):
    run = db.get(EvalRunRecord, run_id)
    if run is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 评测运行 {run_id} 不存在")
    return {"run_id": run.id, "agent": run.agent, "dataset": run.dataset,
            "verdict": run.verdict, "judge": run.judge, "min_pass_rate": run.min_pass_rate,
            "scores": run.scores}


async def _invoke(db: Session, agent: str, text: str) -> dict:
    from ...agents.registry import registry

    resp = await registry.invoke(db, agent, InvokeRequest(input=text))
    return {"output": resp.output}

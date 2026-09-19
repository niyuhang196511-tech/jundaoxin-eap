"""评测中心 API（v0.6 深化）：数据集 + 异步评测运行 + 多维裁判 + 回归对比 + RAG 评测。

- 数据集 kind：agent（input + expected_any/expectation）| rag（query + relevant_chunk_ids）
- 裁判：rule（关键词）/ llm（LLM-as-Judge，多维 1-5 分 + 总体 passed，judge_model 可配）
- 执行：eval.run 任务（后台异步），运行历史列表 + 详情 + 回归对比（对齐 docs/08 §3）
- RAG 指标：HitRate@K / Recall@K / MRR / NDCG（确定性纯函数，见 runtime/rag_eval.py）
"""

from __future__ import annotations

import json
import uuid

import fastapi
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...observability import audit
from ...models import EvalDatasetRecord, EvalRunRecord
from ...schemas import InvokeRequest
from ..deps import require_admin, require_api_key, resolve_tenant

router = fastapi.APIRouter(prefix="/api/v1/evals",
                           dependencies=[fastapi.Depends(resolve_tenant), fastapi.Depends(require_api_key)])


class EvalDatasetCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    description: str = ""
    kind: str = Field(default="agent", pattern=r"^(agent|rag)$",
                      description="agent=智能体问答用例；rag=检索标注用例")
    cases: list[dict] = Field(min_length=1,
                              description='agent: [{"input","expected_any"|"expectation"}]；'
                                          'rag: [{"query","relevant_chunk_ids":[...]}]')


class EvalRunRequest(BaseModel):
    agent: str = ""
    dataset: str
    min_pass_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    judge: str = Field(default="rule", pattern=r"^(rule|llm)$",
                       description="rule=关键词命中；llm=LLM-as-Judge 多维评分")
    top_k: int = Field(default=5, ge=1, le=20, description="RAG 评测的检索条数")


@router.post("/datasets", dependencies=[fastapi.Depends(require_admin)])
def create_dataset(body: EvalDatasetCreate, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    if body.kind == "agent":
        for i, case in enumerate(body.cases):
            if "input" not in case or not ("expected_any" in case or "expectation" in case):
                raise fastapi.HTTPException(
                    status_code=400,
                    detail=f"EAP-4000 用例 {i} 缺少 input/expected_any（或 llm 裁判用的 expectation）字段")
    else:
        from ...runtime.rag_eval import validate_rag_cases

        try:
            validate_rag_cases(body.cases)
        except ValueError as e:
            raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 {e}") from e
    if db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == body.name)):
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 数据集 {body.name} 已存在")
    record = EvalDatasetRecord(name=body.name, description=body.description,
                               kind=body.kind, cases=body.cases)
    db.add(record)
    db.commit()
    audit.record("eval.dataset.create", actor=audit.actor_of(request), target=body.name,
                 detail={"cases": len(record.cases), "kind": body.kind},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": record.name, "kind": record.kind, "cases": len(record.cases)}


@router.get("/datasets")
def list_datasets(db: Session = fastapi.Depends(get_db)):
    return [
        {"name": d.name, "kind": d.kind, "description": d.description, "cases": len(d.cases)}
        for d in db.scalars(select(EvalDatasetRecord)).all()
    ]


@router.post("/runs", dependencies=[fastapi.Depends(require_admin)])
async def run_evaluation(body: EvalRunRequest, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    """异步评测：提交 eval.run 任务立即返回 run_id（PENDING→RUNNING→PASS/FAIL）。"""
    engine = request.app.state.task_engine

    dataset = db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == body.dataset))
    if dataset is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 数据集 {body.dataset} 不存在")
    if dataset.kind == "rag":
        _require_agent(db, body.agent)
    run = EvalRunRecord(id=uuid.uuid4().hex, agent=body.agent, dataset=body.dataset,
                        kind=dataset.kind, verdict="PENDING",
                        min_pass_rate=body.min_pass_rate, judge=body.judge)
    db.add(run)
    db.commit()
    payload = {"run_id": run.id, "agent": body.agent, "dataset": body.dataset,
               "judge": body.judge, "min_pass_rate": body.min_pass_rate, "top_k": body.top_k}
    await engine.submit(db, "eval.run", payload)
    audit.record("eval.run.submit", actor=audit.actor_of(request), target=run.id,
                 detail={"agent": body.agent, "dataset": body.dataset, "kind": dataset.kind},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"run_id": run.id, "status": "PENDING"}


@router.post("/rag-runs", dependencies=[fastapi.Depends(require_admin)])
async def run_rag_evaluation(body: EvalRunRequest, request: fastapi.Request,
                             db: Session = fastapi.Depends(get_db)):
    """RAG 评测（语义化端点）：等价于 runs（kind=rag 数据集自动走检索指标）。"""
    return await run_evaluation(body, request, db)


def _require_agent(db: Session, agent: str) -> None:
    if not agent:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 RAG 评测需指定知识库（agent 传 KB 名）")


@router.get("/runs")
def list_runs(agent: str | None = None, limit: int = 50, db: Session = fastapi.Depends(get_db)):
    query = select(EvalRunRecord).order_by(EvalRunRecord.created_at.desc()).limit(min(limit, 200))
    if agent:
        query = query.where(EvalRunRecord.agent == agent)
    return [
        {"run_id": r.id, "agent": r.agent, "dataset": r.dataset, "kind": r.kind,
         "verdict": r.verdict, "judge": r.judge, "pass_rate": r.pass_rate,
         "metrics": r.metrics, "created_at": str(r.created_at)}
        for r in db.scalars(query).all()
    ]


async def execute_evaluation(db: Session, agent: str, dataset_name: str,
                             min_pass_rate: float = 0.8, judge: str = "rule") -> dict:
    """评测执行（发布门禁复用）：rule=关键词命中；llm=LLM-as-Judge 多维评分。"""
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
                        verdict=verdict, min_pass_rate=min_pass_rate, judge=judge, scores=scores,
                        pass_rate=round(rate, 4))
    db.add(run)
    db.commit()
    return {"run_id": run.id, "agent": agent, "dataset": dataset_name, "judge": judge,
            "verdict": verdict, "pass_rate": round(rate, 4), "scores": scores}


def _keywords_criteria(case: dict) -> str:
    kws = "、".join(case.get("expected_any", []))
    return f"回答需体现以下关键词或其语义：{kws}" if kws else "回答应切题且符合客服规范"


async def _llm_judge(db: Session, case_input: str, output: str, expectation: str) -> dict:
    """LLM-as-Judge 多维评分（v0.6）：正确性/相关性/格式各 1-5 分 + 总体 passed。"""
    from ...config import get_settings
    from ...modelhub.router import hub

    system = ("EAP-JUDGE 你是严格的评测裁判。依据评分标准从三个维度打分（正确性/相关性/格式，各 1-5 分），"
              "并给出总体结论。只输出一个 JSON 对象，禁止输出其他任何文字："
              '{"passed": true, "reason": "简短理由", '
              '"scores": {"correctness": 4, "relevance": 5, "format": 4}}')
    user = (f"【评分标准】\n{expectation}\n\n【用户输入】\n{case_input}\n\n"
            f"【智能体输出】\n{output[:2000]}")
    prefer = get_settings().judge_model or None
    try:
        completion = await hub.complete(
            db, [{"role": "system", "content": system}, {"role": "user", "content": user}],
            capability="chat", prefer=prefer)
        raw = completion.result.content or ""
    except Exception as e:
        return {"passed": False, "reason": f"裁判调用失败: {str(e)[:120]}"}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"passed": False, "reason": "裁判输出无法解析为 JSON", "raw": raw[:160]}
    try:
        parsed = json.loads(raw[start:end + 1])
        scores = parsed.get("scores") or {}
        return {"passed": bool(parsed.get("passed")), "reason": str(parsed.get("reason", ""))[:200],
                "scores": {k: scores.get(k) for k in ("correctness", "relevance", "format")}}
    except json.JSONDecodeError:
        return {"passed": False, "reason": "裁判输出无法解析为 JSON", "raw": raw[:160]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, compare: str | None = None, db: Session = fastapi.Depends(get_db)):
    """运行详情；compare=<prev_run_id> 时输出逐指标回归对比（劣化 >1pt 标记，docs/08 §3）。"""
    run = db.get(EvalRunRecord, run_id)
    if run is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 评测运行 {run_id} 不存在")
    result = {"run_id": run.id, "agent": run.agent, "dataset": run.dataset, "kind": run.kind,
              "verdict": run.verdict, "judge": run.judge, "min_pass_rate": run.min_pass_rate,
              "pass_rate": run.pass_rate, "metrics": run.metrics, "scores": run.scores}
    if compare:
        prev = db.get(EvalRunRecord, compare)
        if prev is None:
            raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 对比运行 {compare} 不存在")
        cur_metrics = {"pass_rate": run.pass_rate or 0.0, **(run.metrics or {})}
        prev_metrics = {"pass_rate": prev.pass_rate or 0.0, **(prev.metrics or {})}
        keys = sorted(set(cur_metrics) | set(prev_metrics))
        regression = []
        for k in keys:
            cur_v, prev_v = float(cur_metrics.get(k) or 0), float(prev_metrics.get(k) or 0)
            delta = round(cur_v - prev_v, 4)
            regression.append({"metric": k, "current": cur_v, "previous": prev_v,
                               "delta": delta, "regressed": delta < -0.01})
        result["regression"] = {"compare_to": compare, "metrics": regression,
                                "any_regression": any(r["regressed"] for r in regression)}
    return result


async def _invoke(db: Session, agent: str, text: str) -> dict:
    from ...agents.registry import registry

    resp = await registry.invoke(db, agent, InvokeRequest(input=text))
    return {"output": resp.output}

"""评测中心 API（v0.6 深化）：数据集 + 异步评测运行 + 多维裁判 + 回归对比 + RAG 评测。

- 数据集 kind：agent（input + expected_any/expectation）| rag（query + relevant_chunk_ids）
- 裁判：rule（关键词）/ llm（LLM-as-Judge，多维 1-5 分 + 总体 passed，judge_model 可配）
- 执行：eval.run 任务（后台异步），运行历史列表 + 详情 + 回归对比（对齐 docs/08 §3）
- RAG 指标：HitRate@K / Recall@K / MRR / NDCG（确定性纯函数，见 runtime/rag_eval.py）
- 模型直评（M42-B）：run 请求带 model 时直接评测模型（不经过 agent，用例逐条经 hub
  prefer=model 调用），结果落 eval_runs.model 列，作为 eval-gate 路由门禁的判定输入
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
from ...models import (AgentRecord, EvalDatasetRecord, EvalRunRecord, ReviewSampleRecord,
                       ShadowConfigRecord, ShadowRunRecord, TaskRecord)
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
    model: str = Field(default="", description="M42-B 模型直评：指定模型名时直接评测该模型（agent 须留空）")
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
    """异步评测：提交 eval.run 任务立即返回 run_id（PENDING→RUNNING→PASS/FAIL）。

    M42-B 模型直评：body.model 非空时评测对象为模型本身（agent 留空，仅支持 agent 数据集）。
    """
    engine = request.app.state.task_engine

    dataset = db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == body.dataset))
    if dataset is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 数据集 {body.dataset} 不存在")
    if body.model:
        if body.agent:
            raise fastapi.HTTPException(status_code=400,
                                        detail="EAP-4000 agent 与 model 二选一：模型直评时 agent 须留空")
        if dataset.kind != "agent":
            raise fastapi.HTTPException(
                status_code=400,
                detail="EAP-4000 模型直评仅支持 agent 数据集（rag 评测经知识库检索，与模型无关）")
    elif dataset.kind == "rag":
        _require_agent(db, body.agent)
    run = EvalRunRecord(id=uuid.uuid4().hex, agent=body.agent, dataset=body.dataset,
                        model=body.model or None, kind=dataset.kind, verdict="PENDING",
                        min_pass_rate=body.min_pass_rate, judge=body.judge)
    db.add(run)
    db.commit()
    payload = {"run_id": run.id, "agent": body.agent, "model": body.model, "dataset": body.dataset,
               "judge": body.judge, "min_pass_rate": body.min_pass_rate, "top_k": body.top_k}
    await engine.submit(db, "eval.run", payload)
    audit.record("eval.run.submit", actor=audit.actor_of(request), target=run.id,
                 detail={"agent": body.agent, "model": body.model,
                         "dataset": body.dataset, "kind": dataset.kind},
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
def list_runs(agent: str | None = None, model: str | None = None, limit: int = 50,
              db: Session = fastapi.Depends(get_db)):
    """运行历史；agent/model 可选过滤（M42-B：按模型筛模型直评记录）。"""
    query = select(EvalRunRecord).order_by(EvalRunRecord.created_at.desc()).limit(min(limit, 200))
    if agent:
        query = query.where(EvalRunRecord.agent == agent)
    if model:
        query = query.where(EvalRunRecord.model == model)
    return [
        {"run_id": r.id, "agent": r.agent, "model": r.model or "", "dataset": r.dataset,
         "kind": r.kind, "verdict": r.verdict, "judge": r.judge, "pass_rate": r.pass_rate,
         "metrics": r.metrics, "created_at": str(r.created_at)}
        for r in db.scalars(query).all()
    ]


async def execute_evaluation(db: Session, agent: str, dataset_name: str,
                             min_pass_rate: float = 0.8, judge: str = "rule") -> dict:
    """评测执行（发布门禁复用）：rule=关键词命中；llm=LLM-as-Judge 多维评分。"""
    dataset = db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == dataset_name))
    if dataset is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 数据集 {dataset_name} 不存在")

    async def produce_output(case: dict) -> str:
        resp = await _invoke(db, agent, case["input"])
        return resp["output"]

    return await _finish_evaluation(db, dataset, produce_output, min_pass_rate, judge, agent=agent)


async def execute_model_evaluation(db: Session, model: str, dataset_name: str,
                                   min_pass_rate: float = 0.8, judge: str = "rule") -> dict:
    """模型直评（M42-B，M45-B 修正直连）：不经过 agent，用例逐条经 hub 直连目标模型调用。

    only_prefer=True 钉死链为目标模型一个元素——首选模型供应商失败时直抛错误
    （逐用例落 error 判失败），绝不落到链上其他模型应答（否则直评测到的不是
    目标模型本身，门禁输入失真）。评分/判定/落库复用 agent 评测的同一流程
    （_finish_evaluation），结果落 model 列，作为 eval-gate 路由门禁的判定输入。
    eval.run 在任务引擎内执行（平台内部模式，无租户上下文），路由链不受
    eval-gate 策略自我拦截。
    """
    from ...modelhub.router import hub

    dataset = db.scalar(select(EvalDatasetRecord).where(EvalDatasetRecord.name == dataset_name))
    if dataset is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 数据集 {dataset_name} 不存在")

    async def produce_output(case: dict) -> str:
        completion = await hub.complete(db, [{"role": "user", "content": case["input"]}],
                                        capability="chat", prefer=model, only_prefer=True)
        return completion.result.content or ""

    return await _finish_evaluation(db, dataset, produce_output, min_pass_rate, judge, model=model)


async def _finish_evaluation(db: Session, dataset: EvalDatasetRecord,
                             produce_output, min_pass_rate: float, judge: str,
                             *, agent: str = "", model: str | None = None) -> dict:
    """逐用例评分 → verdict/pass_rate 判定 → EvalRunRecord 落库（agent/模型直评共用）。"""
    scores = []
    passed = 0
    for case in dataset.cases:
        try:
            output = await produce_output(case)
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
    run = EvalRunRecord(id=uuid.uuid4().hex, agent=agent, dataset=dataset.name, model=model,
                        verdict=verdict, min_pass_rate=min_pass_rate, judge=judge, scores=scores,
                        pass_rate=round(rate, 4))
    db.add(run)
    db.commit()
    return {"run_id": run.id, "agent": agent, "model": model or "", "dataset": dataset.name,
            "judge": judge, "verdict": verdict, "pass_rate": round(rate, 4), "scores": scores}


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
    result = {"run_id": run.id, "agent": run.agent, "model": run.model or "",
              "dataset": run.dataset, "kind": run.kind,
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




# ---------- 在线影子流量（M44-A，L10 评测深化）：影子配置 + 配对运行 + 对比报表 ----------

class ShadowConfigCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    source_agent: str = Field(min_length=1, description="生产 agent（调用完成点触发分流）")
    shadow_agent: str = Field(min_length=1, description="影子 agent（候选版本，输出不返回用户）")
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0, description="抽样率（每请求独立判定）")
    judge_criteria: str = Field(default="", max_length=2000,
                                description="报表期 LLM 裁判评分标准（空 = 报表无 judge 对比）")
    note: str = Field(default="", max_length=512)


class ShadowConfigPatch(BaseModel):
    shadow_agent: str | None = None
    sample_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_criteria: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=512)


def _shadow_config_or_404(db: Session, name: str) -> ShadowConfigRecord:
    row = db.scalar(select(ShadowConfigRecord).where(ShadowConfigRecord.name == name))
    if row is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 影子配置 {name} 不存在")
    return row


def _agent_exists(db: Session, name: str, role: str) -> None:
    if db.scalar(select(AgentRecord).where(AgentRecord.name == name)) is None:
        raise fastapi.HTTPException(status_code=400,
                                    detail=f"EAP-4000 {role} agent {name} 不存在（先创建/注册）")


@router.post("/shadow-configs", dependencies=[fastapi.Depends(require_admin)])
def create_shadow_config(body: ShadowConfigCreate, request: fastapi.Request,
                         db: Session = fastapi.Depends(get_db)):
    """创建影子流量配置（admin+审计 shadow.create）：source/shadow agent 须已存在。"""
    _agent_exists(db, body.source_agent, "生产")
    _agent_exists(db, body.shadow_agent, "影子")
    if db.scalar(select(ShadowConfigRecord).where(ShadowConfigRecord.name == body.name)) is not None:
        raise fastapi.HTTPException(status_code=409, detail=f"EAP-2002 影子配置 {body.name} 已存在")
    row = ShadowConfigRecord(name=body.name, source_agent=body.source_agent,
                             shadow_agent=body.shadow_agent, sample_rate=body.sample_rate,
                             judge_criteria=body.judge_criteria, note=body.note)
    db.add(row)
    db.commit()
    audit.record("shadow.create", actor=audit.actor_of(request), target=body.name,
                 detail={"source": body.source_agent, "shadow": body.shadow_agent,
                         "sample_rate": body.sample_rate},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": row.name, "source_agent": row.source_agent,
            "shadow_agent": row.shadow_agent, "sample_rate": row.sample_rate,
            "enabled": row.enabled}


@router.get("/shadow-configs")
def list_shadow_configs(db: Session = fastapi.Depends(get_db)):
    rows = db.scalars(select(ShadowConfigRecord).order_by(ShadowConfigRecord.id.desc())).all()
    return [{"name": r.name, "source_agent": r.source_agent, "shadow_agent": r.shadow_agent,
             "sample_rate": r.sample_rate, "enabled": r.enabled, "note": r.note,
             "judge": bool(r.judge_criteria), "created_at": str(r.created_at)} for r in rows]


@router.patch("/shadow-configs/{name}", dependencies=[fastapi.Depends(require_admin)])
def patch_shadow_config(name: str, body: ShadowConfigPatch, request: fastapi.Request,
                        db: Session = fastapi.Depends(get_db)):
    row = _shadow_config_or_404(db, name)
    if body.shadow_agent is not None:
        _agent_exists(db, body.shadow_agent, "影子")
        row.shadow_agent = body.shadow_agent
    if body.sample_rate is not None:
        row.sample_rate = body.sample_rate
    if body.judge_criteria is not None:
        row.judge_criteria = body.judge_criteria
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.note is not None:
        row.note = body.note
    db.commit()
    audit.record("shadow.update", actor=audit.actor_of(request), target=name,
                 detail={"enabled": row.enabled, "sample_rate": row.sample_rate},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": row.name, "enabled": row.enabled, "sample_rate": row.sample_rate}


@router.delete("/shadow-configs/{name}", dependencies=[fastapi.Depends(require_admin)])
def delete_shadow_config(name: str, request: fastapi.Request, db: Session = fastapi.Depends(get_db)):
    row = _shadow_config_or_404(db, name)
    db.delete(row)  # shadow_runs 保留（历史对比数据不随配置删除）
    db.commit()
    audit.record("shadow.delete", actor=audit.actor_of(request), target=name,
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"name": name, "status": "deleted"}


@router.get("/shadow-runs")
def list_shadow_runs(config: str | None = None, limit: int = 50,
                     db: Session = fastapi.Depends(get_db)):
    q = select(ShadowRunRecord).order_by(ShadowRunRecord.id.desc()).limit(min(limit, 200))
    if config:
        q = q.where(ShadowRunRecord.config_name == config)
    rows = db.scalars(q).all()
    return [{"id": r.id, "config": r.config_name, "trace_id": r.trace_id,
             "source_agent": r.source_agent, "shadow_agent": r.shadow_agent,
             "input": r.input_text[:200], "primary_latency_ms": r.primary_latency_ms,
             "shadow_latency_ms": r.shadow_latency_ms,
             "shadow_ok": not r.shadow_error, "shadow_error": r.shadow_error,
             "created_at": str(r.created_at)} for r in rows]


@router.get("/shadow-runs/{run_id}")
def get_shadow_run(run_id: int, db: Session = fastapi.Depends(get_db)):
    r = db.get(ShadowRunRecord, run_id)
    if r is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 影子运行 {run_id} 不存在")
    return {"id": r.id, "config": r.config_name, "trace_id": r.trace_id,
            "source_agent": r.source_agent, "shadow_agent": r.shadow_agent,
            "input": r.input_text, "primary_output": r.primary_output,
            "primary_latency_ms": r.primary_latency_ms, "shadow_output": r.shadow_output,
            "shadow_latency_ms": r.shadow_latency_ms,
            "shadow_ok": not r.shadow_error, "shadow_error": r.shadow_error,
            "created_at": str(r.created_at)}


@router.get("/shadow-configs/{name}/report")
async def shadow_report(name: str, request: fastapi.Request, judge: bool = False,
                        limit: int = 20, db: Session = fastapi.Depends(get_db)):
    """影子对比报表（M44-A）：总量/影子成功率/延迟均值与中位数/输出一致率；
    judge=true 时对最近 N 对成功影子运行做 LLM 裁判评分（两侧分别评，对比通过率）。"""
    cfg = _shadow_config_or_404(db, name)
    if judge and not cfg.judge_criteria:
        raise fastapi.HTTPException(
            status_code=400, detail="EAP-4000 该配置未设置 judge_criteria（评分标准），无法做裁判对比")
    rows = db.scalars(select(ShadowRunRecord).where(ShadowRunRecord.config_name == name)
                      .order_by(ShadowRunRecord.id.desc()).limit(1000)).all()
    total = len(rows)
    ok_rows = [r for r in rows if not r.shadow_error]
    if total == 0:
        return {"config": name, "source_agent": cfg.source_agent,
                "shadow_agent": cfg.shadow_agent, "sample_rate": cfg.sample_rate,
                "report": {"total": 0, "note": "暂无影子运行"}}

    def _avg(vals: list[int]) -> float:
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    def _p50(vals: list[int]) -> int:
        return sorted(vals)[len(vals) // 2] if vals else 0

    same = sum(1 for r in ok_rows
               if (r.shadow_output or "").strip() == (r.primary_output or "").strip())
    report = {
        "total": total,
        "shadow_ok": len(ok_rows),
        "shadow_fail": total - len(ok_rows),
        "shadow_fail_rate": round((total - len(ok_rows)) / total, 4) if total else 0.0,
        "primary_latency_avg_ms": _avg([r.primary_latency_ms for r in ok_rows]),
        "shadow_latency_avg_ms": _avg([r.shadow_latency_ms for r in ok_rows]),
        "primary_latency_p50_ms": _p50([r.primary_latency_ms for r in ok_rows]),
        "shadow_latency_p50_ms": _p50([r.shadow_latency_ms for r in ok_rows]),
        "exact_match_rate": round(same / len(ok_rows), 4) if ok_rows else 0.0,
        "exact_match_note": "输出完全一致率（朴素口径，语义等价不计入；语义对比用 judge=true）",
    }
    if judge:
        pairs = ok_rows[: max(1, min(limit, 50))]
        judge_rows = []
        for r in pairs:
            p = await _llm_judge(db, r.input_text, r.primary_output, cfg.judge_criteria)
            s = await _llm_judge(db, r.input_text, r.shadow_output, cfg.judge_criteria)
            judge_rows.append({"shadow_run_id": r.id, "primary_judge": p, "shadow_judge": s})

        def _pass_avg(items: list[dict]) -> float:
            return round(sum(1 for j in items if j.get("passed")) / len(items), 4) if items else 0.0

        report["judge"] = {
            "criteria": cfg.judge_criteria[:200],
            "pairs": len(judge_rows),
            "primary_pass_rate": _pass_avg([j["primary_judge"] for j in judge_rows]),
            "shadow_pass_rate": _pass_avg([j["shadow_judge"] for j in judge_rows]),
            "details": judge_rows,
        }
        audit.record("shadow.report.judge", actor=audit.actor_of(request), target=name,
                     detail={"pairs": len(judge_rows)},
                     trace_id=getattr(request.state, "trace_id", ""))
    return {"config": name, "source_agent": cfg.source_agent,
            "shadow_agent": cfg.shadow_agent, "sample_rate": cfg.sample_rate,
            "report": report}


# ---------- 人工抽检（M44-B，L10 评测深化）：任务抽样 → 人工评分 → 报表聚合 ----------

_REVIEW_DIMS = ("correctness", "relevance", "format")


class ReviewSampleCreate(BaseModel):
    task_id: str = Field(min_length=1, description="从 agent.invoke/agent.hitl 任务快照抽样")


class ReviewSubmit(BaseModel):
    scores: dict = Field(default_factory=dict,
                         description="维度评分，键限 correctness/relevance/format，值 1-5（可只评部分维度）")
    note: str = Field(default="", max_length=2000)


def _review_sample_or_404(db: Session, sample_id: int) -> ReviewSampleRecord:
    row = db.get(ReviewSampleRecord, sample_id)
    if row is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 抽检样本 {sample_id} 不存在")
    return row


@router.post("/reviews/sample", dependencies=[fastapi.Depends(require_admin)])
def sample_review(body: ReviewSampleCreate, request: fastapi.Request,
                  db: Session = fastapi.Depends(get_db)):
    """从任务快照抽样进入人工抽检队列（admin+审计 eval.review.sample）。

    仅 agent.invoke/agent.hitl 任务可抽（有 input/output 语义）；同源任务防重复入选。
    """
    task = db.get(TaskRecord, body.task_id)
    if task is None:
        raise fastapi.HTTPException(status_code=404, detail=f"EAP-4004 任务 {body.task_id} 不存在")
    if task.type not in ("agent.invoke", "agent.hitl"):
        raise fastapi.HTTPException(status_code=400,
                                    detail=f"EAP-4000 任务类型 {task.type} 不支持抽检（仅 agent.invoke/agent.hitl）")
    dup = db.scalar(select(ReviewSampleRecord).where(
        ReviewSampleRecord.source == "task", ReviewSampleRecord.source_id == task.id))
    if dup is not None:
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-2002 任务 {task.id} 已在抽检队列（样本 #{dup.id}）")
    agent = str((task.payload or {}).get("agent") or "")
    if not agent:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 任务 payload 缺少 agent 字段，无法归属抽检")
    row = ReviewSampleRecord(
        agent=agent, source="task", source_id=task.id,
        input_text=str((task.payload or {}).get("input", ""))[:8000],
        output_text=str((task.result or {}).get("output", ""))[:8000],
        sampled_by=audit.actor_of(request))
    db.add(row)
    db.commit()
    audit.record("eval.review.sample", actor=audit.actor_of(request), target=task.id,
                 detail={"sample_id": row.id, "agent": agent, "state": task.state},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"id": row.id, "agent": row.agent, "source": row.source, "source_id": row.source_id,
            "status": row.status, "task_state": task.state, "created_at": str(row.created_at)}


@router.get("/reviews")
def list_reviews(status: str | None = None, agent: str | None = None, limit: int = 50,
                 db: Session = fastapi.Depends(get_db)):
    q = select(ReviewSampleRecord).order_by(ReviewSampleRecord.id.desc()).limit(min(limit, 200))
    if status:
        q = q.where(ReviewSampleRecord.status == status)
    if agent:
        q = q.where(ReviewSampleRecord.agent == agent)
    rows = db.scalars(q).all()
    return [{"id": r.id, "agent": r.agent, "status": r.status, "source_id": r.source_id,
             "input": r.input_text[:200], "output": r.output_text[:200],
             "scores": r.scores, "note": r.note, "reviewed_by": r.reviewed_by,
             "created_at": str(r.created_at)} for r in rows]


@router.get("/reviews/report")
def review_report(agent: str | None = None, db: Session = fastapi.Depends(get_db)):
    """人工抽检报表：抽样/评审量 + 各维度均值 + 好评率（全部已评维度均 ≥4 记正向）。"""
    q = select(ReviewSampleRecord)
    if agent:
        q = q.where(ReviewSampleRecord.agent == agent)
    rows = db.scalars(q).all()
    reviewed = [r for r in rows if r.status == "reviewed"]
    def _dim_avg(dim: str) -> float | None:
        vals = [float(r.scores[dim]) for r in reviewed if dim in (r.scores or {})]
        return round(sum(vals) / len(vals), 2) if vals else None
    positive = sum(1 for r in reviewed
                   if (r.scores or {}) and all(v >= 4 for v in r.scores.values()))
    return {"agent": agent or "*",
            "total": len(rows), "reviewed": len(reviewed), "pending": len(rows) - len(reviewed),
            "avg_scores": {dim: _dim_avg(dim) for dim in _REVIEW_DIMS},
            "positive_rate": round(positive / len(reviewed), 4) if reviewed else None,
            "note": "positive_rate=已评样本中全部维度 ≥4 的占比（人工口径，与 LLM 裁判分位不同）"}


@router.get("/reviews/{sample_id}")
def get_review(sample_id: int, db: Session = fastapi.Depends(get_db)):
    r = _review_sample_or_404(db, sample_id)
    return {"id": r.id, "agent": r.agent, "status": r.status, "source": r.source,
            "source_id": r.source_id, "input": r.input_text, "output": r.output_text,
            "scores": r.scores, "note": r.note, "sampled_by": r.sampled_by,
            "reviewed_by": r.reviewed_by, "created_at": str(r.created_at),
            "reviewed_at": str(r.reviewed_at) if r.reviewed_at else None}


@router.post("/reviews/{sample_id}/review", dependencies=[fastapi.Depends(require_admin)])
def submit_review(sample_id: int, body: ReviewSubmit, request: fastapi.Request,
                  db: Session = fastapi.Depends(get_db)):
    """提交人工评分（admin+审计 eval.review.submit）：维度键与分值校验，pending → reviewed。"""
    row = _review_sample_or_404(db, sample_id)
    if row.status == "reviewed":
        raise fastapi.HTTPException(status_code=409,
                                    detail=f"EAP-2002 样本 #{sample_id} 已评审完成（不可重复评审）")
    scores = {}
    for dim, val in (body.scores or {}).items():
        if dim not in _REVIEW_DIMS:
            raise fastapi.HTTPException(status_code=400,
                                        detail=f"EAP-4000 未知评分维度 {dim}（限 {'/'.join(_REVIEW_DIMS)}）")
        if not isinstance(val, (int, float)) or not (1 <= val <= 5):
            raise fastapi.HTTPException(status_code=400, detail=f"EAP-4000 维度 {dim} 分值须为 1-5")
        scores[dim] = float(val)
    if not scores:
        raise fastapi.HTTPException(status_code=400, detail="EAP-4000 scores 至少包含一个维度评分")
    from datetime import datetime, timezone

    row.scores = scores
    row.note = body.note
    row.status = "reviewed"
    row.reviewed_by = audit.actor_of(request)
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    audit.record("eval.review.submit", actor=audit.actor_of(request), target=f"#{sample_id}",
                 detail={"agent": row.agent, "scores": scores},
                 trace_id=getattr(request.state, "trace_id", ""))
    return {"id": row.id, "status": row.status, "scores": row.scores,
            "reviewed_by": row.reviewed_by}

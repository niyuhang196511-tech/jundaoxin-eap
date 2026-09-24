"""示例：经任务引擎跑长任务（M48-B 示例库，Task Engine 接入形态）。

演示任务引擎（docs/03 §5：TaskRecord 8 态状态机 + 租约/取消/HITL）的开发者用法：
- register_handler(task_type, handler)——Handler = async (payload, prev_result) -> dict
- engine.submit(db, task_type, payload, priority, idempotency_key) -> task_id
- worker 异步执行，状态/结果落 TaskRecord，API 可查/可取消/可审批

平台内置任务类型（agent.invoke / agent.hitl / kb.ingest / eval.run）经同一个
create_task_engine() 注册（eap/runtime/tasks.py），与本示例的自定义类型并存；
HTTP 通道：POST /api/v1/tasks {"type": ..., "payload": ...} 即提交同款任务。

独立体验（在 eap/ 目录；未设置 EAP_DB_URL 时自动用一次性临时库）：
    uv run python examples/task-flow/task_flow_demo.py
"""

from __future__ import annotations

import asyncio

TASK_TYPE = "demo.long_run"


async def long_run_handler(payload: dict, prev_result: dict) -> dict:
    """长任务执行体：分步推进；重跑（崩溃恢复/重试）时从已完成步骤续推。

    - payload：提交时传入的任务参数
    - prev_result：重跑前的结果快照（首次执行为 {}）——用它实现断点续跑
    - 返回值：写入 TaskRecord.result（GET /api/v1/tasks/{id} 可查）
    """
    steps = max(1, int(payload.get("steps", 5)))
    done = list(prev_result.get("done", []))
    for i in range(len(done), steps):
        await asyncio.sleep(0.05)  # 模拟真实耗时工作（文档解析 / 向量入库 / 外部 API…）
        done.append(f"step-{i + 1}")
    return {"done": done, "echo": payload.get("echo", ""), "steps": steps}


async def run_demo(steps: int = 5) -> dict:
    """端到端演示：注册 handler → 启动引擎 → 提交 → 轮询到终态 → 停机。"""
    from eap.db import SessionLocal, init_db
    from eap.models import TaskRecord
    from eap.runtime.tasks import create_task_engine

    init_db()
    engine = create_task_engine()
    engine.register_handler(TASK_TYPE, long_run_handler)
    await engine.start(workers=1)
    try:
        with SessionLocal() as db:
            task_id = str(await engine.submit(
                db, TASK_TYPE, {"steps": steps, "echo": "hello task-flow"}))
        for _ in range(300):  # 最多等 ~15s（示例任务实际 <1s）
            with SessionLocal() as db:
                record = db.get(TaskRecord, task_id)
                if record is not None and record.state in ("COMPLETED", "FAILED", "CANCELLED"):
                    return {"task_id": task_id, "state": record.state, "result": record.result}
            await asyncio.sleep(0.05)
        return {"task_id": task_id, "state": "TIMEOUT", "result": {}}
    finally:
        await engine.stop()


if __name__ == "__main__":
    import os
    import tempfile

    # 示例默认不碰业务库：EAP_DB_URL 未显式配置时用一次性临时库
    #（get_settings 为 lru_cache，须在首次导入 eap.db 前设置——本文件对 eap 均为懒导入）
    os.environ.setdefault("EAP_DB_URL",
                          f"sqlite:///{tempfile.mkdtemp(prefix='eap-task-demo-')}/demo.db")
    snapshot = asyncio.run(run_demo())
    print(f"task {snapshot['task_id']} → {snapshot['state']}")
    print(snapshot["result"])

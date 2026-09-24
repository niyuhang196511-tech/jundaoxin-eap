# task-flow — 经任务引擎跑长任务示例（可运行）

演示任务引擎（docs/03 §5）的开发者接入形态：自定义任务类型经
`register_handler` 注册后走统一队列/8 态状态机（PENDING → RUNNING →
COMPLETED/FAILED/CANCELLED，含租约恢复与幂等提交）。

## 文件

- `task_flow_demo.py`：`long_run_handler`（分步推进、断点续跑）+ `run_demo()` 端到端脚本

## 运行（在 `eap/` 目录）

```bash
uv run python examples/task-flow/task_flow_demo.py
```

未设置 `EAP_DB_URL` 时自动使用一次性临时库（不碰业务库）。预期输出：

```
[tasks] 引擎启动：AsyncioQueueBackend × 1 workers
task <id> → COMPLETED
{'done': ['step-1', 'step-2', 'step-3', 'step-4', 'step-5'], 'echo': 'hello task-flow', 'steps': 5}
```

## 与平台的对应关系

| 示例代码 | 平台形态 |
| --- | --- |
| `engine.register_handler("demo.long_run", handler)` | 内置 `agent.invoke` / `agent.hitl` / `kb.ingest` / `eval.run` 同机制（`eap/runtime/tasks.py`） |
| `engine.submit(db, TASK_TYPE, payload)` | `POST /api/v1/tasks {"type": "demo.long_run", "payload": {...}}` |
| 轮询 `TaskRecord.state` | `GET /api/v1/tasks/{id}`（列表 `GET /api/v1/tasks?state=`） |
| —— | 取消 `POST /api/v1/tasks/{id}/cancel`；审批 `/approve`；交互输入 `/interact` |

- **断点续跑**：handler 从 `prev_result["done"]` 续推，对应租约过期崩溃恢复
  （RUNNING 超租约 → PENDING 重跑）与重试语义。
- **幂等提交**：`submit(..., idempotency_key=...)` 在途任务直接命中返回（M32）。
- **定时任务**：`POST /api/v1/tasks/schedules` 按 cron/interval 自动提交同款任务。

## 下一步

把 `long_run_handler` 替换为你的耗时工作（批量文档解析、向量库重建、外部系统同步…），
即获得带可观测状态机、可取消、可恢复的生产级长任务。

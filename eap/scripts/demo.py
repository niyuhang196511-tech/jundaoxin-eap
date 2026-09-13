"""EAP 平台一键导览：进程内跑通全部核心模块（无需启动服务、完全离线）。

用法：cd eap && uv run python scripts/demo.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

from fastapi.testclient import TestClient  # noqa: E402

from eap.main import create_app  # noqa: E402

H = {"Authorization": "Bearer dev-key-1", "Content-Type": "application/json"}
client = TestClient(create_app())


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n■ {title}\n{'=' * 60}")


def call(method: str, url: str, body=None, expect=200):
    r = client.request(method, url, headers=H, json=body)
    allowed = expect if isinstance(expect, tuple) else (expect,)
    assert r.status_code in allowed, f"{method} {url} → {r.status_code}: {r.text[:200]}"
    return r.json() if r.content else {}


def main() -> None:
    section("0 · 平台健康")
    print(json.dumps(call("GET", "/health"), ensure_ascii=False))

    section("1 · 模型中心：OpenAI 兼容对话（mock 离线）")
    r = call("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "你好"}]})
    print(f"路由到 {r['model']} → {r['choices'][0]['message']['content'][:40]}")

    section("2 · 知识中心：混合检索（BM25+向量 RRF）+ Citation")
    resp = call("POST", "/api/v1/kb/website-faq/retrieve", {"query": "如何创建知识库", "top_k": 2})
    for h in resp["hits"]:
        print(f"  [{h['citation']['document']}] {h['content'][:50]}")

    section("3 · 智能体调用（注册钩子纳管，带引用）")
    r = call("POST", "/api/v1/agents/faq-agent/invocations", {"input": "如何创建知识库？"})
    print(f"输出: {r['output'][:60]}  引用 {len(r['citations'])} 条")

    section("4 · 多智能体：supervisor 委派 faq/order 子智能体")
    r = call("POST", "/api/v1/agents/support-supervisor/invocations",
             {"input": "帮我看看怎么创建知识库"})
    print(f"汇总: {r['output'][:60]}  步骤 {len(r['steps'])} 步")

    section("5 · Workflow：并行分支 + 子流程（DSL 即智能体）")
    call("POST", "/api/v1/workflows", {
        "name": "demo-flow", "version": "1.0.0", "steps": [
            {"id": "fanout", "type": "parallel", "branches": [
                {"id": "search", "steps": [{"id": "r", "type": "retrieve", "kb": "website-faq"}]},
                {"id": "think", "steps": [{"id": "l", "type": "llm", "system": "简短分析"}]},
            ]},
            {"id": "answer", "type": "llm", "system": "汇总作答：$fanout"},
        ]}, expect=(200, 409))
    r = call("POST", "/api/v1/agents/demo-flow/invocations", {"input": "如何创建知识库？"})
    for s in r["steps"][:4]:
        print(f"  {s}")

    section("6 · 记忆：写入 → 召回 → 遗忘")
    call("POST", "/api/v1/memory", {"scope": "user", "user_id": "demo-u",
                                    "content": "客户偏好周一下单，走顺丰", "kind": "preference"})
    recalled = call("POST", "/api/v1/memory/recall",
                    {"scope": "user", "user_id": "demo-u", "query": "物流偏好"})["hits"]
    print(f"召回 {len(recalled)} 条: {recalled[0]['content'] if recalled else '-'}")

    section("7 · 发布治理：草案 → 门禁评测 → 灰度 → prod")
    rel = call("POST", "/api/v1/releases", {"agent": "faq-agent", "version": "9.9.0"},
               expect=(200, 409))
    if rel.get("id") is None:  # 重复执行：取已存在的发布
        rel = next(r for r in call("GET", "/api/v1/releases", None)
                   if r["agent"] == "faq-agent" and r["version"] == "9.9.0")
    if rel["state"] == "prod":
        print("该发布已是 prod（幂等跳过生命周期演示）")
    else:
        if rel["state"] == "draft":
            call("POST", f"/api/v1/releases/{rel['id']}/promote")
        gate = call("POST", f"/api/v1/releases/{rel['id']}/eval",
                    {"dataset": "faq-smoke", "min_pass_rate": 0.8, "judge": "llm"})
        print(f"llm 裁判门禁: {gate['eval_verdict']}（通过率 {gate['pass_rate']}）")
        if rel["state"] in ("draft", "review"):
            call("POST", f"/api/v1/releases/{rel['id']}/canary", {"percent": 20})
        state_now = next(r for r in call("GET", "/api/v1/releases", None) if r["id"] == rel["id"])
        print(f"发布态: {state_now['state']}"
              + (f"（灰度 {state_now['canary_percent']}%）" if state_now["state"] == "canary" else ""))
        if state_now["state"] == "canary":
            hit = None
            for i in range(8):  # 稳定 hash 分流：命中与否取决于 user_id，多试几个用户
                u = f"canary-check-{i}"
                r = call("POST", "/api/v1/agents/faq-agent/invocations",
                         {"input": "灰度验证", "user_id": u})
                if r["canary"]:
                    hit = u
                    break
            print(f"灰度命中用户: {hit or '（前 8 个用户未命中，比例 20% 属正常）'}")
            call("POST", f"/api/v1/releases/{rel['id']}/promote")  # 灰度转正
            print("已转正 prod（旧版退役）")

    section("8 · 成本中心：用量与预算")
    summary = call("GET", "/api/v1/budgets/1/summary")
    print(f"本月 {summary['calls']} 次调用 / {summary['tokens_total']} tokens")

    section("9 · 策略中心：数据不出域（仅本地供应商）")
    call("POST", "/api/v1/policies", {"name": "demo-local-only", "tenant_id": 1,
                                      "kind": "provider-allowlist",
                                      "config": {"providers": ["mock"]}, "priority": 10},
         expect=(200, 409))
    r = call("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
    print(f"策略下路由到: {r['model']}（openai_compat 供应商被过滤）")
    call("POST", "/api/v1/policies/demo-local-only/enabled?enabled=false")

    section("10 · 企业连接器：mock-erp 工具注入 + HITL 下单")
    tools = call("GET", "/api/v1/connectors/mock-erp/tools", expect=200)
    print(f"连接器工具: {[t['function']['name'] for t in tools]}")
    task = call("POST", "/api/v1/tasks",
                {"type": "agent.hitl", "payload": {"agent": "order-agent",
                                                   "input": "帮我下一台 EAP 一体机"}})
    tid = task["task_id"]
    import time
    for _ in range(30):
        t = call("GET", f"/api/v1/tasks/{tid}")
        if t["state"] in ("WAITING_HUMAN", "COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    print(f"HITL: 任务 {t['state']}（挂起工具 {t.get('pending_tool', '-')}）")
    call("POST", f"/api/v1/tasks/{tid}/approve", {"decision": True})
    for _ in range(30):
        t = call("GET", f"/api/v1/tasks/{tid}")
        if t["state"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    print(f"批准后: {t['state']}")

    section("11 · 技能包：签名导出 → 验签导入")
    bundle = call("GET", "/api/v1/skills/customer-service/package")
    print(f"签名 {len(bundle['signature'])} 字符，SKILL.md {len(bundle['skill_md'])} 字符")
    r = call("POST", "/api/v1/skills/import", {"bundle": bundle})
    print(f"导入: {r['status']}（默认停用待审）")

    section("12 · Prompt 流水线 + A/B")
    call("POST", "/api/v1/prompts/faq-answer-style/versions",
         {"version": "9.9.0", "template": "精简模式：{{question}}", "notes": "demo"},
         expect=(200, 409))
    call("POST", "/api/v1/prompts/experiments",
         {"name": "demo-style-exp", "prompt": "faq-answer-style",
          "version_a": "1.0.0", "version_b": "9.9.0", "percent_b": 100},
         expect=(200, 409))
    r = call("POST", "/api/v1/prompts/faq-answer-style/render",
             {"variables": {"question": "q"}, "key": "demo-key"})
    print(f"A/B 命中: {r['version']}（{(r['experiment'] or {}).get('picked', '指针')} 桶）")

    section("完成 ✔ 全部模块导览结束（进程内运行，未产生任何网络请求）")


if __name__ == "__main__":
    with TestClient(create_app()) as _client:  # with 触发 lifespan（建库/种子/智能体注册）
        client = _client
        main()

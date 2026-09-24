"""M41-A 组织记忆联动 KB 测试（docs/18 §五）：沉淀端点挑选/幂等/KB 文档生成/ACL 联动/
检索可见/无候选行为/RBAC 非 admin 403。全部离线确定性（本地 hash 嵌入、mock IdP）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_jwt_auth import _access_token  # noqa: F401 复用 JWT 构造
from .test_oidc import fake_idp  # noqa: F401,F811 复用模拟 IdP 夹具（fixture 再导出）


def _write_org(client: TestClient, content: str, importance: float) -> int:
    resp = client.post("/api/v1/memory", headers=AUTH, json={
        "scope": "org", "kind": "fact", "content": content, "importance": importance})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_consolidate_flow_idempotent_acl_retrieve(client: TestClient):
    """主流程：org 记忆高/低重要性混合 → 沉淀只收高阈值 → KB 文档生成且内容含条目 →
    再次调用 consolidated=0（幂等）→ ACL 规则存在且 /retrieve 命中。"""
    # 预清扫：把库内既有未沉淀的高重要性 org 记忆先沉淀掉（不影响本测断言，保证确定性）
    sweep = client.post("/api/v1/memory/consolidate", headers=AUTH, json={})
    assert sweep.status_code == 200, sweep.text
    assert sweep.json()["kb"] == "org-memory"

    id_high1 = _write_org(client, "发布会前所有演示环境必须完成冒烟测试核对清单", 0.9)
    id_high2 = _write_org(client, "客户续约需提前 30 天由客户成功团队跟进", 0.85)
    id_low = _write_org(client, "低重要性备忘：周三例会常规同步进度", 0.3)

    resp = client.post("/api/v1/memory/consolidate", headers=AUTH, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consolidated"] == 2, body
    assert body["kb"] == "org-memory"
    assert body["document_id"]
    assert body["title"].startswith("组织记忆沉淀 "), body["title"]

    # 文档生成（经既有摄入管线：分块/嵌入/图谱）
    docs = client.get(f"/api/v1/kb/{body['kb']}/documents", headers=AUTH).json()
    mine = [d for d in docs if d["id"] == body["document_id"]]
    assert mine and mine[0]["title"] == body["title"]
    assert mine[0]["meta"]["type"] == "memory-consolidate"
    assert sorted(mine[0]["meta"]["memory_ids"]) == sorted([id_high1, id_high2])
    chunks = client.get(
        f"/api/v1/kb/{body['kb']}/documents/{body['document_id']}/chunks", headers=AUTH).json()
    chunk_text = "\n".join(c["content"] for c in chunks)
    assert "冒烟测试" in chunk_text and "客户成功团队" in chunk_text, "正文应逐条列出记忆内容"
    assert "周三例会" not in chunk_text, "低于阈值的记忆不应入选"

    # 幂等：再次调用 → 0，不新建文档
    n_docs = len(docs)
    again = client.post("/api/v1/memory/consolidate", headers=AUTH, json={})
    assert again.status_code == 200
    assert again.json()["consolidated"] == 0 and again.json()["document_id"] is None
    assert len(client.get(f"/api/v1/kb/{body['kb']}/documents", headers=AUTH).json()) == n_docs

    # ACL 联动：沉淀文档带 role=* allow 规则
    acls = client.get(f"/api/v1/kb/{body['kb']}/acls", headers=AUTH).json()
    rule = [a for a in acls if a["document_id"] == body["document_id"]]
    assert rule and rule[0]["effect"] == "allow" \
        and rule[0]["subject_type"] == "role" and rule[0]["subject"] == "*"

    # 检索可见：org 记忆内容可被 /retrieve 命中
    hits = client.post(f"/api/v1/kb/{body['kb']}/retrieve", headers=AUTH,
                       json={"query": "演示环境 冒烟测试", "top_k": 3}).json()["hits"]
    assert hits and any("冒烟测试" in h["content"] for h in hits)
    assert any(h["citation"]["document"] == body["title"] for h in hits), \
        "命中引用应指向沉淀文档"

    for mid in (id_high1, id_high2, id_low):
        client.delete(f"/api/v1/memory/{mid}", headers=AUTH)


def test_consolidate_no_candidates_no_kb_created(client: TestClient):
    """无可沉淀记忆（阈值 1.0 无候选）：返回 {consolidated: 0}，不建文档也不建 KB。"""
    kb_name = "org-mem-none-" + "x" * 8  # 合法名（KBCreate 同名规则），且不会与其他测冲突
    resp = client.post("/api/v1/memory/consolidate", headers=AUTH,
                       json={"kb_name": kb_name, "min_importance": 1.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consolidated"] == 0
    assert body["document_id"] is None and body["title"] is None
    # KB 未被自动创建（无可沉淀内容不落空库）
    assert client.get(f"/api/v1/kb/{kb_name}/documents",
                      headers=AUTH).status_code == 404
    # kb_name 不满足 KBCreate 命名规则 → 422
    bad = client.post("/api/v1/memory/consolidate", headers=AUTH,
                      json={"kb_name": "Bad_KB_Name!"})
    assert bad.status_code == 422


def test_consolidate_rbac_member_403(client: TestClient, fake_idp):  # noqa: F811 参数仅为激活夹具（模块级导入供 pytest 发现）
    """RBAC：member JWT 调用沉淀端点 → 403（admin 语义，与 purge 一致）。"""
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    resp = client.post("/api/v1/memory/consolidate", headers=member, json={})
    assert resp.status_code == 403, resp.text

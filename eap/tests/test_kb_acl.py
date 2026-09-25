"""文档级 ACL 测试（M36/L6，设计 docs/17）：判定纯函数 / 检索过滤 / 端点与存量兼容。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


# ---------- 纯函数判定（docs/17 §三） ----------

def test_is_allowed_semantics():
    from eap.knowledge.acl import AclContext, is_allowed

    admin = AclContext(tenant_id=1, roles=("admin",))
    member = AclContext(tenant_id=1, user_id="u-1", roles=("member",))
    # 默认可见（无规则）
    assert is_allowed([], member)
    # deny 命中 → 拒绝（deny 优先，即使有 allow）
    rules = [{"effect": "deny", "subject_type": "role", "subject": "member"},
             {"effect": "allow", "subject_type": "role", "subject": "member"}]
    assert not is_allowed(rules, member)
    # allow 命中 → 允许；未命中 → 默认可见（默认可见语义：allow 在单规则集内不收窄，
    # 白名单收窄作用在 filter_doc_ids 的文档级覆盖 KB 级结构上——见下一测试）
    wl = [{"effect": "allow", "subject_type": "user", "subject": "u-1"}]
    assert is_allowed(wl, AclContext(tenant_id=1, user_id="u-1"))
    assert is_allowed(wl, AclContext(tenant_id=1, user_id="u-2"))
    # "*" 通配 deny：全员拒绝
    assert not is_allowed([{"effect": "deny", "subject_type": "role", "subject": "*"}], admin)
    # role 主体不消费 user_id（user_id 同名不等于角色命中）——未命中 → 默认可见
    assert is_allowed([{"effect": "allow", "subject_type": "role", "subject": "u-1"}],
                      AclContext(tenant_id=1, user_id="u-1"))


def test_kb_default_rule_overridden_by_doc_rule():
    """KB 级默认规则作用于全部文档；文档级规则存在时覆盖默认（filter_doc_ids）。"""
    from eap.knowledge.acl import AclContext, filter_doc_ids

    class _R:  # 模拟 load_rules 返回形态
        pass

    import eap.knowledge.acl as acl_mod

    member = AclContext(tenant_id=1, roles=("member",))
    acl_token = acl_mod.set_acl_context(member)
    rules = [{"effect": "deny", "subject_type": "role", "subject": "member", "document_id": None}]
    monkey_owner = acl_mod._cache
    try:
        acl_mod._cache[7] = (__import__("time").monotonic(), rules)
        allowed, filtered = filter_doc_ids(None, 7, [11, 12, 13])
        assert allowed == set() and filtered == 3
        # 文档 12 有自己的 allow → 覆盖 KB 级 deny
        rules2 = rules + [{"effect": "allow", "subject_type": "role", "subject": "member",
                           "document_id": 12}]
        acl_mod._cache[7] = (__import__("time").monotonic(), rules2)
        allowed, filtered = filter_doc_ids(None, 7, [11, 12, 13])
        assert allowed == {12} and filtered == 2
    finally:
        monkey_owner.clear()
        acl_mod.reset_acl_context(acl_token)


# ---------- 检索集成（三路单点过滤 + 存量兼容） ----------

def _ingest_doc(client: TestClient, kb: str, title: str, text: str) -> None:
    r = client.post(f"/api/v1/kb/{kb}/documents", headers=HEADERS, json={
        "title": title, "text": text})
    assert r.status_code == 200, r.text


def _add_acl(client: TestClient, kb: str, effect: str, subject: str,
             document_id: int | None = None):
    r = client.post(f"/api/v1/kb/{kb}/acls", headers=HEADERS, json={
        "effect": effect, "subject_type": "role", "subject": subject,
        "document_id": document_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_retrieve_filters_denied_document(client: TestClient, uname):
    """deny 文档不出现在命中与引用；无规则 KB 存量兼容（全部可见）。"""
    kb = uname("acl-demo")  # M52-D：KB 名唯一化——重名 409，且检索断言只面对本遍文档
    assert client.post("/api/v1/kb", headers=HEADERS,
                       json={"name": kb, "title": "ACL 演示"}).status_code == 200
    _ingest_doc(client, kb, "公开手册", "退货政策：七天无理由退货，需要提供订单号。")
    _ingest_doc(client, kb, "机密薪资表", "薪资机密：张三月薪五万，李四月薪三万，严禁外泄。")

    # 存量兼容：无规则 → 两篇都可见
    r = client.post(f"/api/v1/kb/{kb}/retrieve", headers=HEADERS,
                    json={"query": "月薪 退货政策", "top_k": 5})
    assert r.status_code == 200
    docs = {h["citation"]["document"] for h in r.json()["hits"]}
    assert {"公开手册", "机密薪资表"} <= docs

    # deny 机密文档（role=member）→ 命中与引用同步消失（dev-key-1 为 admin 通道，
    # admin 不受 member deny 影响——验证 role 维度判定而非通道）
    acl_id = _add_acl(client, kb, "deny", "admin", document_id=None)
    r = client.post(f"/api/v1/kb/{kb}/retrieve", headers=HEADERS,
                    json={"query": "月薪", "top_k": 5})
    assert r.json()["hits"] == []  # KB 级 deny admin → admin 全部不可见
    client.delete(f"/api/v1/kb/acls/{acl_id}", headers=HEADERS)

    # 文档级 deny：仅机密文档被过滤，公开文档仍可检索
    docs_resp = client.get(f"/api/v1/kb/{kb}/documents", headers=AUTH).json()
    secret_id = next(d["id"] for d in docs_resp if d["title"] == "机密薪资表")
    _add_acl(client, kb, "deny", "admin", document_id=secret_id)
    r = client.post(f"/api/v1/kb/{kb}/retrieve", headers=HEADERS,
                    json={"query": "月薪 薪资", "top_k": 5})
    docs = {h["citation"]["document"] for h in r.json()["hits"]}
    assert "机密薪资表" not in docs
    r = client.post(f"/api/v1/kb/{kb}/retrieve", headers=HEADERS,
                    json={"query": "退货政策", "top_k": 5})
    docs = {h["citation"]["document"] for h in r.json()["hits"]}
    assert "公开手册" in docs


def test_acl_crud_rbac_and_validation(client: TestClient, uname):
    """ACL 端点：CRUD、RBAC（非 admin 403）、坏文档 404、删除后列表同步。"""
    kb = uname("acl-crud")  # M52-D：KB 名唯一化（重名 409；ACL 计数断言只面对本遍规则）
    assert client.post("/api/v1/kb", headers=HEADERS,
                       json={"name": kb, "title": "ACL CRUD"}).status_code == 200
    acl_id = _add_acl(client, kb, "allow", "vip-user")
    listing = client.get(f"/api/v1/kb/{kb}/acls", headers=AUTH).json()
    assert len(listing) == 1 and listing[0]["document"] == "(整库默认)"
    # 删除
    assert client.delete(f"/api/v1/kb/acls/{acl_id}", headers=HEADERS).json()["status"] == "deleted"
    assert client.get(f"/api/v1/kb/{kb}/acls", headers=AUTH).json() == []
    # 坏文档 404
    r = client.post(f"/api/v1/kb/{kb}/acls", headers=HEADERS, json={
        "effect": "deny", "subject_type": "role", "subject": "member", "document_id": 99999})
    assert r.status_code == 404
    # 坏 effect 422（pydantic）
    assert client.post(f"/api/v1/kb/{kb}/acls", headers=HEADERS, json={
        "effect": "maybe", "subject_type": "role", "subject": "x"}).status_code == 422


def test_graph_overview_filters_denied(client: TestClient, uname):
    """图谱概览：被 deny 文档的节点过滤（按过滤后计）。"""
    kb = uname("acl-graph")  # M52-D：KB 名唯一化（重名 409）
    assert client.post("/api/v1/kb", headers=HEADERS,
                       json={"name": kb, "title": "ACL 图谱"}).status_code == 200
    _ingest_doc(client, kb, "公开产品说明", "EAP 平台支持模型路由与知识检索，覆盖多租户。")
    docs = client.get(f"/api/v1/kb/{kb}/documents", headers=AUTH).json()
    doc_id = docs[0]["id"]
    _add_acl(client, kb, "deny", "admin", document_id=doc_id)
    g = client.get(f"/api/v1/kb/{kb}/graph", headers=AUTH).json()
    leaked = [n for n in g.get("nodes", []) if isinstance(n, dict) and n.get("document_id") == doc_id]
    assert leaked == []

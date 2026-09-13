"""知识图谱路：实体共现建图、多跳召回、三路 RRF 融合、级联删除（docs/04 §1.3）。"""

from __future__ import annotations

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


def _create_kb(client, name):
    r = client.post("/api/v1/kb", headers=HEADERS, json={"name": name, "title": name, "template": "doc"})
    assert r.status_code in (200, 409)
    return next(k for k in client.get("/api/v1/kb", headers=HEADERS).json() if k["name"] == name)


def test_graph_build_and_multi_hop_recall(client):
    """A-B 共现建图；查询锚定 A 经图扩展召回 B、C（B 与 A 共享实体，C 与 B 共享实体）。"""
    kb = _create_kb(client, "graph-kb")
    # A：顺丰与 EAP一体机 共现
    client.post(f"/api/v1/kb/{kb['name']}/documents", headers=HEADERS,
                json={"title": "物流说明", "text": "EAP一体机 走顺丰发货，顺丰次日达。"})
    # B：退货与 EAP一体机 共现（与 A 无直接文本重叠的关键词）
    client.post(f"/api/v1/kb/{kb['name']}/documents", headers=HEADERS,
                json={"title": "退货说明", "text": "EAP一体机 三十天无理由退货，退货走顺丰到付。"})
    # C：发票与 退货 共现（与 A 无共享词，需 2 跳才可达）
    client.post(f"/api/v1/kb/{kb['name']}/documents", headers=HEADERS,
                json={"title": "发票说明", "text": "退货完成后可开具红字发票，发票随退货单寄出。"})

    # 图谱概览有节点有边
    g = client.get(f"/api/v1/kb/{kb['name']}/graph", headers=HEADERS).json()
    assert g["nodes"] and g["edges"]

    # 查询锚定"顺丰"（只在 A 出现）→ 图扩展应把 B（共现 EAP一体机）也召回
    hits = client.post(f"/api/v1/kb/{kb['name']}/retrieve", headers=HEADERS,
                       json={"query": "顺丰 发货", "top_k": 5}).json()["hits"]
    docs = {h["citation"]["document"] for h in hits}
    assert "物流说明" in docs and "退货说明" in docs, docs

    # 查询锚定"发票"（只在 C 出现）→ 2 跳内可达 A/B
    hits = client.post(f"/api/v1/kb/{kb['name']}/retrieve", headers=HEADERS,
                       json={"query": "发票 怎么开", "top_k": 5}).json()["hits"]
    docs = {h["citation"]["document"] for h in hits}
    assert "发票说明" in docs and ("退货说明" in docs or "物流说明" in docs), docs


def test_graph_cascade_delete(client):
    kb = _create_kb(client, "graph-del")
    r = client.post(f"/api/v1/kb/{kb['name']}/documents", headers=HEADERS,
                    json={"title": "d1", "text": "EAP一体机 走顺丰。"})
    doc_id = r.json()["document_id"]
    before = client.get(f"/api/v1/kb/{kb['name']}/graph", headers=HEADERS).json()
    assert before["edges"]
    # 删除文档 → 边清空、孤立节点清理
    client.delete(f"/api/v1/kb/{kb['name']}/documents/{doc_id}", headers=HEADERS)
    after = client.get(f"/api/v1/kb/{kb['name']}/graph", headers=HEADERS).json()
    assert after["edges"] == [] and after["nodes"] == []


def test_existing_kb_flow_unaffected(client):
    """三路融合不破坏既有行为：website-faq 常规问答仍以相关性排序命中。"""
    hits = client.post("/api/v1/kb/website-faq/retrieve", headers=HEADERS,
                       json={"query": "如何创建知识库", "top_k": 2}).json()["hits"]
    assert hits and "知识库" in hits[0]["content"]

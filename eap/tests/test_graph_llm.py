"""图谱 LLM 抽取：EAP_GRAPH_EXTRACTION=llm 的模型抽取与失败回退（docs/04 §1 升级路径）。

离线约定：mock 模型对 EAP-GRAPH 标记返回确定性抽取——输入中的 ASCII 词为实体、
相邻建关系；纯中文内容抽取为空 → 自动回退词元共现。
"""

from __future__ import annotations

import pytest

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}


@pytest.fixture()
def llm_mode(monkeypatch):
    """llm 抽取模式：env 设置 + 缓存清理，测试结束恢复（真 fixture 保证 teardown）。"""
    import os

    monkeypatch.setenv("EAP_GRAPH_EXTRACTION", "llm")
    from eap.config import get_settings

    get_settings.cache_clear()
    yield
    monkeypatch.delenv("EAP_GRAPH_EXTRACTION", raising=False)
    get_settings.cache_clear()


def test_llm_extraction_creates_typed_relations(client, llm_mode):
    client.post("/api/v1/kb", headers=HEADERS,
                json={"name": "graph-llm-kb", "title": "graph-llm-kb", "template": "doc"})
    client.post("/api/v1/kb/graph-llm-kb/documents", headers=HEADERS,
                json={"title": "订单流", "text": "下单后 order 与 inventory 联动更新 stock 数据。"})
    g = client.get("/api/v1/kb/graph-llm-kb/graph", headers=HEADERS).json()
    # mock 抽取：ASCII 词（order/inventory/stock）为实体、相邻建边
    assert {"order", "inventory", "stock"} <= set(g["nodes"])
    assert any(e["relation"] == "co" for e in g["edges"]), g["edges"]


def test_llm_extraction_failure_falls_back_to_cooccurrence(client, llm_mode):
    """纯中文内容 mock 抽取为空 → 自动回退词元共现（图谱不为空）。"""
    client.post("/api/v1/kb", headers=HEADERS,
                json={"name": "graph-fallback-kb", "title": "graph-fallback-kb", "template": "doc"})
    client.post("/api/v1/kb/graph-fallback-kb/documents", headers=HEADERS,
                json={"title": "中文文档", "text": "退货政策说明：三十天无理由退货，运费自理。"})
    g = client.get("/api/v1/kb/graph-fallback-kb/graph", headers=HEADERS).json()
    assert g["nodes"], "回退后应仍有共现实体"
    assert g["edges"], "回退后应仍有共现边"


def test_lexical_mode_default_unchanged(client):
    """默认 lexical 模式：行为与既有共现图一致。"""
    client.post("/api/v1/kb", headers=HEADERS,
                json={"name": "graph-lex-kb", "title": "graph-lex-kb", "template": "doc"})
    client.post("/api/v1/kb/graph-lex-kb/documents", headers=HEADERS,
                json={"title": "d", "text": "EAP一体机 走顺丰。"})
    g = client.get("/api/v1/kb/graph-lex-kb/graph", headers=HEADERS).json()
    assert g["edges"] and all(e["relation"] == "" for e in g["edges"])

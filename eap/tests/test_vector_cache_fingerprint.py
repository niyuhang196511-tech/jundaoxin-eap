"""M53-A：LocalVectorStore 磁盘缓存内容指纹回归（跨库碰撞真 bug）。

根因（M52-D 批次实测登记）：磁盘 npz 旧失效键 (chunk 数, 首/尾 chunk id) 全是每库各自
的自增 id，且文件名只有 kb_id——换库/同路径重建库时 id 序列复现，旧库矩阵被当作新库
加载，余弦分张冠李戴 → 检索排序头部翻转（修复前实证：投毒缓存下 test_rerank 确定性红）。

本文件单元级验证磁盘层指纹校验，不建 KB 不脏库：
- 缓存目录经 media_dir monkeypatch 隔离到 tmp_path（绝不写脏仓库 vector-cache/）；
- 进程级 _cache 每测试置换为空 dict 并在断言点 clear()——真实碰撞发生在进程边界
  （两次 pytest 运行/两个 DB 文件），测试以 clear 模拟进程重启；
- kb_id 取 99xxxx 段唯一值，即使隔离失效也不与真实库文件互踩（脏环境可重入）。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from eap.config import get_settings
from eap.knowledge.vector_store import LocalVectorStore

Q = [1.0, 0.0, 0.0, 0.0]  # 查询向量：只看第一维分量，便于构造对抗性缓存


def _chunk(cid: int, kb_id: int, content: str, emb: list[float]) -> SimpleNamespace:
    """轻量假 Chunk（scores_for/指纹只触 id/kb_id/content/embedding 四个属性）。"""
    return SimpleNamespace(id=cid, kb_id=kb_id, content=content, embedding=list(emb))


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """缓存目录隔离：media_dir/../vector-cache → tmp_path/vector-cache。

    同时置换类级 _cache（测毕还原）——既隔离他测残留，也提供「进程重启」语义。
    """
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(LocalVectorStore, "_cache", {})
    return tmp_path / "vector-cache"


def test_cross_db_id_replay_does_not_hit_stale_cache(cache_dir):
    """碰撞回归：同 kb_id、同 (chunk 数, 首 id, 尾 id) 但内容不同的两个「库」，
    第二库加载必须 miss 重建——修复前会命中第一库的 stale 矩阵（分数张冠李戴）。"""
    kb = 990001
    # 库 A：中间块与查询成 0.6 分量
    chunks_a = [
        _chunk(1, kb, "首块内容A", [1.0, 0.0, 0.0, 0.0]),
        _chunk(2, kb, "中间块A", [0.6, 0.8, 0.0, 0.0]),
        _chunk(3, kb, "尾块内容A", [0.0, 1.0, 0.0, 0.0]),
    ]
    scores_a = LocalVectorStore().scores_for(chunks_a, Q)
    assert scores_a[1] == pytest.approx(0.6)
    assert (cache_dir / f"{kb}.npz").exists()

    # 库 B：id 序列完全复现（1,2,3——旧键 (3, 3, 1) 必撞），内容/向量不同
    LocalVectorStore._cache.clear()  # 模拟进程重启（真实碰撞跨进程边界）
    chunks_b = [
        _chunk(1, kb, "首块内容B", [1.0, 0.0, 0.0, 0.0]),
        _chunk(2, kb, "中间块B", [0.0, 0.0, 1.0, 0.0]),  # 与查询正交 → 正确分 0.0
        _chunk(3, kb, "尾块内容B", [0.0, 1.0, 0.0, 0.0]),
    ]
    scores_b = LocalVectorStore().scores_for(chunks_b, Q)
    # 修复前：加载库 A 矩阵 → scores_b[1] == 0.6（张冠李戴）；修复后：指纹不符重建
    assert scores_b[1] == pytest.approx(0.0), "命中了别库 stale 磁盘缓存（指纹校验失效）"
    assert scores_b[0] == pytest.approx(1.0)
    # 重建后 npz 以新格式覆写：指纹 = 当前库实算值
    with np.load(cache_dir / f"{kb}.npz") as data:
        assert "fingerprint" in data.files
        assert str(data["fingerprint"]) == LocalVectorStore()._fingerprint(kb, chunks_b)


def test_same_content_disk_hit_skips_rebuild(cache_dir, monkeypatch):
    """命中保持：同库同内容「进程重启」后二次加载命中磁盘缓存，不重建矩阵。

    探针：_persist 只应在首次（重建落盘）被调用一次；命中路径零调用零重嵌入。
    """
    kb = 990002
    chunks = [
        _chunk(1, kb, "内容一", [1.0, 0.0, 0.0, 0.0]),
        _chunk(2, kb, "内容二", [0.6, 0.8, 0.0, 0.0]),
    ]
    persist_calls: list[int] = []
    orig_persist = LocalVectorStore._persist
    monkeypatch.setattr(
        LocalVectorStore, "_persist",
        lambda self, *a: persist_calls.append(a[0]) or orig_persist(self, *a))

    first = LocalVectorStore().scores_for(chunks, Q)
    assert persist_calls == [kb]  # 首次：miss → 重建 + 落盘

    LocalVectorStore._cache.clear()  # 模拟进程重启
    second = LocalVectorStore().scores_for(chunks, Q)
    assert persist_calls == [kb], "磁盘命中路径不应触发重建落盘"
    assert second == first
    assert second[1] == pytest.approx(0.6)
    assert not list(cache_dir.glob(".*.tmp.npz")), "原子写不应残留 tmp 文件"


def test_legacy_npz_without_fingerprint_is_miss(cache_dir):
    """旧格式 npz（无 fingerprint 字段）一律视为 miss：不报错、按真实嵌入重建、
    并以新格式原子覆写——向后兼容 = 安全失效（存量缓存升级零手工清理）。"""
    kb = 990003
    chunks = [
        _chunk(1, kb, "内容一", [1.0, 0.0, 0.0, 0.0]),
        _chunk(2, kb, "内容二", [0.0, 1.0, 0.0, 0.0]),
    ]
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 手工构造旧格式文件：cache_key (2, 2, 1) 与当前 chunks 精确匹配、矩阵对抗性
    # （行序调换——若被加载则 scores == [0.0, 1.0]，即修复前张冠李戴形态）
    np.savez_compressed(
        cache_dir / f"{kb}.npz",
        matrix=np.asarray([[0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]], dtype="float32"),
        chunk_ids=np.asarray([1, 2], dtype="int64"),
        cache_key=np.asarray([2, 2, 1], dtype="int64"),
    )
    scores = LocalVectorStore().scores_for(chunks, Q)
    assert scores == pytest.approx([1.0, 0.0]), "旧格式缓存必须失效（不得加载对抗矩阵）"
    with np.load(cache_dir / f"{kb}.npz") as data:  # 已被新格式覆写
        assert "fingerprint" in data.files
        assert "cache_key" not in data.files

"""示例：自定义 RAG 重排器（M48-B 示例库，RAG SDK · reranker 契约）。

实现 rerank(query, candidates, params) -> list[int]（候选下标，相关性降序）。
纯函数实现即满足契约（Chunker/Reranker 基类继承可选，鸭子类型兼容）。

注意：本目录名含连字符，不能作为 Python 包导入；平台插件加载器按文件路径
加载 manifest.py（eap/plugins.py 约定入口），与 .eapext 打包安装行为一致——
验证/加载方式见同目录 README.md。
"""

from __future__ import annotations


def keyword_rerank(query: str, candidates: list[tuple[int, str]],
                   params: dict | None = None) -> list[int]:
    """关键词重合度重排：query 分词后逐候选计分（命中词数），分数降序、同分保持原序。

    - query：检索词（示例按空白分词；替换为你的分词器/语义打分即可）
    - candidates：[(候选下标, 文本)]，来自向量/检索阶段的初排结果
    - 返回：候选下标列表（相关性降序），KB 检索管线取前 top_k 消费
    """
    tokens = {t for t in str(query).lower().split() if t}
    if not tokens:
        return [idx for idx, _ in candidates]

    def score(item: tuple[int, str]) -> tuple[int, int]:
        idx, content = item
        hits = sum(1 for t in tokens if t in str(content).lower())
        return (-hits, idx)  # 命中数降序；同分按下标升序（稳定）

    return [idx for idx, _ in sorted(candidates, key=score)]


def _register() -> None:
    from eap.knowledge.components import register_reranker

    register_reranker("keyword-rerank", keyword_rerank,
                      description="示例：关键词重合度重排（query 分词命中计数，离线确定性）",
                      source="plugin")


EAP_PLUGIN = {
    "name": "rag-pipeline",
    "kind": "rag",
    "description": "示例：关键词重合度重排器（可运行，离线确定性）",
    "register": _register,
}

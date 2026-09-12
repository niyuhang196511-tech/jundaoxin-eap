"""知识库检索工具：把 Retriever 包装为 OpenAI function-calling 工具（模型即工具的反向：库即工具）。"""

from __future__ import annotations

import json

from ..runtime.tools import Tool


def retriever_tool(kb_name: str, top_k: int = 4) -> Tool:
    """知识库检索工具：智能体经工具调用查询指定 KB，返回带引用的资料 JSON。"""

    async def handler(arguments: str) -> str:
        from ..db import SessionLocal
        from ..knowledge import service as kb_svc
        from ..models import KB

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        query = str(args.get("query") or "").strip()
        k = int(args.get("top_k") or top_k)
        if not query:
            return json.dumps({"error": "缺少 query 参数"}, ensure_ascii=False)
        with SessionLocal() as db:
            kb = db.query(KB).filter_by(name=kb_name).first()
            if kb is None:
                return json.dumps({"error": f"知识库 {kb_name} 不存在"}, ensure_ascii=False)
            hits = kb_svc.retrieve(db, kb, query, top_k=k)
            return json.dumps({
                "context": [f"[{i}]（{h['citation']['document']}）{h['content']}" for i, h in enumerate(hits, 1)],
                "citations": [h["citation"] for h in hits],
            }, ensure_ascii=False)

    return Tool(
        name=f"kb.{kb_name}.search",
        description=f"检索企业知识库 {kb_name}，返回带来源编号的资料片段（回答时需标注 [n]）",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认 " + str(top_k)},
            },
            "required": ["query"],
        },
        handler=handler,
        emits_citations=True,
    )

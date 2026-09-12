"""分块：段落聚合 + 长段落硬切（带重叠）。FAQ 问答对整条成块（docs/04 §1.2）。"""

from __future__ import annotations

import re


def split_text(text: str, target: int = 400, overlap: int = 60) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        while len(p) > int(target * 1.6):  # 超长段落硬切，保留重叠
            chunks.append(p[:target])
            p = p[target - overlap:]
        if not p:
            continue
        if len(buf) + len(p) + 1 <= target:
            buf = f"{buf}\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks

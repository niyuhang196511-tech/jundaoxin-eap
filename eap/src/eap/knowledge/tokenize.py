"""分词：ASCII 词 + 中文单字与二元组（开发级，生产可换 jieba/分词器服务）。"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"^[a-zA-Z0-9]+$")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    han: list[str] = []
    for tok in _TOKEN_RE.findall((text or "").lower()):
        if _ASCII_RE.match(tok):
            out.append(tok)
        else:
            han.append(tok)
    for i, ch in enumerate(han):
        out.append(ch)
        if i + 1 < len(han):
            out.append(ch + han[i + 1])
    return out

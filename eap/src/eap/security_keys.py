"""凭证哈希：API Key 认证依据（M7 安全加固）。

sha256(key) 足够：API Key 是高熵随机串（非口令），无彩虹表/爆破收益；
认证查询走唯一索引等值匹配，O(1)。
"""

from __future__ import annotations

import hashlib


def key_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()

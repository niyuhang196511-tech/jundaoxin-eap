"""凭证哈希：API Key 认证依据（M7 安全加固）。

sha256(key) 足够：API Key 是高熵随机串（非口令），无彩虹表/爆破收益；
认证查询走唯一索引等值匹配，O(1)。
"""

from __future__ import annotations

import hashlib


def key_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


# ---------- JWT 吊销黑名单（M10；身份微服务迁出预留：读写收敛于此模块） ----------

def revoke_token(token: str, expires_at, reason: str = "") -> None:
    """吊销 JWT：存 token 哈希 + 原过期时刻（到点后黑名单条目失去意义，可被惰性清理）。"""
    from datetime import datetime

    from sqlalchemy.orm import Session as _Session

    from .models import RevokedToken

    # 由调用方传入会话（请求级事务）；这里用独立会话保证吊销即刻生效
    from .db import SessionLocal

    with SessionLocal() as db:
        db.merge(RevokedToken(token_hash=key_hash(token), expires_at=expires_at,
                              revoked_at=datetime.utcnow(), reason=reason))
        db.commit()


def is_revoked(token: str) -> bool:
    from datetime import datetime

    from sqlalchemy import select

    from .db import SessionLocal
    from .models import RevokedToken

    with SessionLocal() as db:
        record = db.get(RevokedToken, key_hash(token))
        if record is None:
            return False
        if record.expires_at < datetime.utcnow():
            db.delete(record)  # 惰性清理：过期的黑名单条目无意义（token 本身已过期）
            db.commit()
            return False
        return True

#!/usr/bin/env python3
"""存量秘密重加密（M48-C 密钥轮换收尾）：旧密钥密文 → 当前密钥（EAP_SECRET_KEY）。

前提：已按 docs/11 §10 轮换流程把旧密钥移入 EAP_SECRET_KEY_PREVIOUS（否则旧密文
解不开，脚本会如实跳过并计数）。执行后所有可解密字段都已是当前密钥密文，
EAP_SECRET_KEY_PREVIOUS 即可移除。

范围（加密字段清单，与 security_crypto.encrypt_secret 的调用点一一对应）：
- ModelRecord.api_key（模型中心供应商密钥）
- ConnectorRecord.api_key / oauth_client_secret_enc / oauth_access_token_enc / oauth_refresh_token_enc
- MCPServerRecord.oauth_client_secret_enc / oauth_access_token_enc（无 refresh 字段）
- IMChannelRecord.app_secret_enc（IM 应用级凭据）
- WebhookEndpointRecord.secret（出站 HMAC 签名密钥）
- TriggerRuleRecord.secret（入站 webhook HMAC 签名密钥）

用法：
    cd eap && uv run python ../scripts/reencrypt_secrets.py [--dry-run]
退出码：0 = 全部成功（或无待处理）；1 = 存在解不开的密文（列明表/行，需人工处理）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eap" / "src"))

# (模型类, 字段名) 清单——与 security_crypto.encrypt_secret 调用点对齐
TARGETS: list[tuple[str, str]] = [
    ("ModelRecord", "api_key"),
    ("ConnectorRecord", "api_key"),
    ("ConnectorRecord", "oauth_client_secret_enc"),
    ("ConnectorRecord", "oauth_access_token_enc"),
    ("ConnectorRecord", "oauth_refresh_token_enc"),
    ("MCPServerRecord", "oauth_client_secret_enc"),
    ("MCPServerRecord", "oauth_access_token_enc"),
    
    ("IMChannelRecord", "app_secret_enc"),
    ("WebhookEndpointRecord", "secret"),
    ("TriggerRuleRecord", "secret"),
]


def reencrypt_all(db, dry_run: bool = False) -> dict:
    """遍历 TARGETS 字段：能解密的密文若非当前密钥密文则用当前密钥重写。

    返回 {"reencrypted": N, "skipped_unreadable": [(表, id, 字段)...], "plain": M}。
    注意：靠「解密回退」无法区分当前/旧密钥密文——统一策略为重写全部可解密值
    （幂等且开销可接受；行数即审计口径）。
    """
    from eap.models import (
        ConnectorRecord, IMChannelRecord, MCPServerRecord, ModelRecord,
        TriggerRuleRecord, WebhookEndpointRecord,
    )
    from eap.security_crypto import decrypt_secret, encrypt_secret, is_encrypted

    model_map = {m.__name__: m for m in (ModelRecord, ConnectorRecord, MCPServerRecord,
                                         IMChannelRecord, WebhookEndpointRecord, TriggerRuleRecord)}
    reencrypted = 0
    unreadable: list[tuple[str, int, str]] = []
    plain = 0
    for table, field in TARGETS:
        model = model_map[table]
        for row in db.query(model).all():
            value = getattr(row, field)
            if not value or not is_encrypted(value):
                plain += 1
                continue
            decrypted = decrypt_secret(value)
            if decrypted is None:
                unreadable.append((table, getattr(row, "id", -1), field))
                continue
            new = encrypt_secret(decrypted)
            if new != value:
                if not dry_run:
                    setattr(row, field, new)
                reencrypted += 1
    if not dry_run:
        db.commit()
    return {"reencrypted": reencrypted, "skipped_unreadable": unreadable, "plain": plain}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="存量秘密重加密到当前 EAP_SECRET_KEY")
    p.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = p.parse_args(argv)

    from eap.db import SessionLocal

    db = SessionLocal()
    try:
        result = reencrypt_all(db, dry_run=args.dry_run)
        print(f"重加密 {result['reencrypted']} 条（dry-run={args.dry_run}），"
              f"明文/未加密 {result['plain']} 条")
        if result["skipped_unreadable"]:
            print("解密失败（密钥不匹配，需人工处理）：")
            for table, row_id, field in result["skipped_unreadable"]:
                print(f"  {table}#{row_id}.{field}")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

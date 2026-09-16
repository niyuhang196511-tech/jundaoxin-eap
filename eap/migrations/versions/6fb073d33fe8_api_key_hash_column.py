"""api key hash column

Revision ID: 6fb073d33fe8
Revises: fd76596b12eb
Create Date: 2026-09-16 18:15:56.619892
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6fb073d33fe8'
down_revision: Union[str, Sequence[str], None] = 'fd76596b12eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hash(plain: str) -> str:
    import hashlib

    return hashlib.sha256(plain.encode()).hexdigest()


def upgrade() -> None:
    """API Key 哈希化：加 key_hash 列 → 存量明文回填哈希 → 明文列置空（兼容期保留列结构）。

    baseline 为 create_all 语义（新库已含 key_hash），故加列/索引均需幂等守卫。
    """
    conn = op.get_bind()
    cols = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(api_keys)")).fetchall()] \
        if conn.dialect.name == "sqlite" else [
        c["name"] for c in sa.inspect(conn).get_columns("api_keys")]

    if "key_hash" not in cols:
        op.add_column('api_keys', sa.Column('key_hash', sa.String(length=64), nullable=True))
    if "key_hash" in cols:
        # 索引幂等：已存在（create_all 建出）则跳过
        idx = [i["name"] for i in sa.inspect(conn).get_indexes("api_keys")]
        if 'ix_api_keys_key_hash' not in idx:
            op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
    if "key" in cols:
        inspector_nullable = next(c["nullable"] for c in sa.inspect(conn).get_columns("api_keys")
                                  if c["name"] == "key")
        if inspector_nullable is False:
            op.alter_column('api_keys', 'key', existing_type=sa.VARCHAR(length=128), nullable=True)

    # 存量明文 → 哈希回填（逐行，避免全表载入）
    rows = conn.execute(sa.text("SELECT id, key FROM api_keys WHERE key IS NOT NULL")).fetchall()
    for row_id, plain in rows:
        conn.execute(sa.text("UPDATE api_keys SET key_hash = :h WHERE id = :i"),
                     {"h": _hash(plain), "i": row_id})
    # 明文清空（认证已切换 key_hash；列保留一个兼容期后由后续迁移删除）
    conn.execute(sa.text("UPDATE api_keys SET key = NULL"))


def downgrade() -> None:
    """Downgrade：哈希不可逆——降级前明文已丢失，仅删列。"""
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_column('api_keys', 'key_hash')
    op.alter_column('api_keys', 'key', existing_type=sa.VARCHAR(length=128), nullable=False)

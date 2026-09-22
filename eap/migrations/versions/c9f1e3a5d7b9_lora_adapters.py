"""lora_adapters: vLLM multi-LoRA 托管注册表

Revision ID: c9f1e3a5d7b9
Revises: a5c7e9b1d3f5
Create Date: 2026-09-22

M42-A（L1 工程部分）：LoRA adapter 托管注册表——name（=vLLM adapter/模型名，唯一）、
base_model、source_path（GPU 宿主机 adapter 目录）、served_as（vLLM 服务名，默认=name）、
status 状态机（registered|loaded|unloaded|failed）、note。加载/卸载经 VllmProvider 调
vLLM 管理端点（--enable-lora），部署见 docs/20。

注：down_revision 接既有 head c1d3e5f7a9b1（a5c7e9b1d3f5 的现子节点）保持单头线性，
下游 d1b3f5a7c9e1（M42-B）已约定接本 revision。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9f1e3a5d7b9'
down_revision: Union[str, Sequence[str], None] = 'c1d3e5f7a9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """lora_adapters 表。幂等（表已存在则跳过，baseline create_all 已含）。"""
    conn = op.get_bind()
    if "lora_adapters" not in sa.inspect(conn).get_table_names():
        op.create_table(
            'lora_adapters',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=64), nullable=False, unique=True, index=True),
            sa.Column('base_model', sa.String(length=128), nullable=False, server_default=''),
            sa.Column('source_path', sa.String(length=512), nullable=False, server_default=''),
            sa.Column('served_as', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('status', sa.String(length=16), nullable=False, server_default='registered'),
            sa.Column('note', sa.String(length=512), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_lora_adapters_status', 'lora_adapters', ['status'])


def downgrade() -> None:
    """Downgrade：删 lora_adapters 表。"""
    conn = op.get_bind()
    if "lora_adapters" in sa.inspect(conn).get_table_names():
        op.drop_table('lora_adapters')

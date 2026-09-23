"""review samples manual evaluation

Revision ID: f7d9b1c3e5a2
Revises: e5b7d9f1a3c7
Create Date: 2026-09-23 14:00:xx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7d9b1c3e5a2'
down_revision: Union[str, Sequence[str], None] = 'e5b7d9f1a3c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """人工抽检（M44-B）：review_samples 表。幂等。"""
    conn = op.get_bind()
    if "review_samples" not in set(sa.inspect(conn).get_table_names()):
        op.create_table(
            'review_samples',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('agent', sa.String(length=64), nullable=False),
            sa.Column('source', sa.String(length=16), nullable=True),
            sa.Column('source_id', sa.String(length=64), nullable=False),
            sa.Column('input_text', sa.Text(), nullable=True),
            sa.Column('output_text', sa.Text(), nullable=True),
            sa.Column('status', sa.String(length=16), nullable=True),
            sa.Column('scores', sa.JSON(), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('sampled_by', sa.String(length=128), nullable=True),
            sa.Column('reviewed_by', sa.String(length=128), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_review_samples_agent', 'review_samples', ['agent'])
        op.create_index('ix_review_samples_source_id', 'review_samples', ['source_id'])
        op.create_index('ix_review_samples_status', 'review_samples', ['status'])


def downgrade() -> None:
    """Downgrade。"""
    conn = op.get_bind()
    if "review_samples" in set(sa.inspect(conn).get_table_names()):
        op.drop_table('review_samples')

"""add assets.is_reference (price-proxy reference flag)

Marks auto-created reference assets that exist only to carry a price proxy's
history, so they're excluded from holdings and the asset list.

Revision ID: c4f6a8b0d2e4
Revises: b2d4f6a8c1e3
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4f6a8b0d2e4"
down_revision: Union[str, None] = "b2d4f6a8c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("is_reference", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("assets", "is_reference")

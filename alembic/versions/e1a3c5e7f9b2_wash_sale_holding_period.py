"""tax_lots.holding_period_start + wash_sale_adjustments.matched_quantity

Wash-sale replacement lots inherit the sold shares' holding period, and a
wash sale now disallows only the loss on shares matched by replacement
purchases, so the adjustment records how many shares it matched.

Revision ID: e1a3c5e7f9b2
Revises: d8b0c2e4f6a8
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1a3c5e7f9b2"
down_revision: Union[str, None] = "d8b0c2e4f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable adds are plain ALTER TABLE on SQLite (no table rebuild needed).
    op.add_column("tax_lots", sa.Column("holding_period_start", sa.Date(), nullable=True))
    op.add_column("wash_sale_adjustments",
                  sa.Column("matched_quantity", sa.Numeric(18, 8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wash_sale_adjustments") as batch:
        batch.drop_column("matched_quantity")
    with op.batch_alter_table("tax_lots") as batch:
        batch.drop_column("holding_period_start")

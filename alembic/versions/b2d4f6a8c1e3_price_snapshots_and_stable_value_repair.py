"""price_snapshots table + stable-value price repair

Adds intraday price readings (per asset / day / session bucket) so the app can
show the portfolio's change since the immediately preceding reading. Also
repairs stable-value holdings (CASH and money-market tickers) whose price was
corrupted to their yfinance ticker value (e.g. CASH → the PGIM Ultra Short Bond
ETF at ~$83) back to $1.00.

Revision ID: b2d4f6a8c1e3
Revises: f7b18b9a8468
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d4f6a8c1e3"
down_revision: Union[str, None] = "f7b18b9a8468"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in sync with market_data.STABLE_VALUE_SYMBOLS — symbols that are always
# $1.00 and must never be priced from yfinance.
_STABLE_VALUE = (
    "CASH", "SWTXX", "SWVXX", "VMFXX", "VMSXX",
    "FDRXX", "SPRXX", "FZFXX", "FDLXX", "FTEXX",
)


def upgrade() -> None:
    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("bucket", sa.String(length=12), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "price_date", "bucket", name="uq_snapshot_asset_date_bucket"),
    )
    op.create_index(op.f("ix_price_snapshots_asset_id"), "price_snapshots", ["asset_id"])
    op.create_index(op.f("ix_price_snapshots_price_date"), "price_snapshots", ["price_date"])

    # Repair stable-value prices that were corrupted to a real ticker value.
    symbols = ", ".join(f"'{s}'" for s in _STABLE_VALUE)
    op.execute(
        f"""
        UPDATE asset_prices SET close_price = 1.0
        WHERE asset_id IN (
            SELECT id FROM assets WHERE UPPER(symbol) IN ({symbols})
        )
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_price_snapshots_price_date"), table_name="price_snapshots")
    op.drop_index(op.f("ix_price_snapshots_asset_id"), table_name="price_snapshots")
    op.drop_table("price_snapshots")

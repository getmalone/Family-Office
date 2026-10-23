"""Tax lot tracking models for the Family Office."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TaxLot(Base, TimestampMixin):
    """An individual tax lot representing a specific acquisition of an asset."""

    __tablename__ = "tax_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    acquisition_date: Mapped[date] = mapped_column(Date)
    acquisition_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    original_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    cost_basis_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    original_cost_basis_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    cost_basis_method: Mapped[str] = mapped_column(String(20), default="fifo")
    is_wash_sale_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    wash_sale_disallowed_loss: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0")
    )
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Start of the holding period when it differs from acquisition_date: a
    # wash-sale replacement lot inherits ("tacks on") the sold shares' period.
    holding_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)

    @property
    def total_cost_basis(self) -> Decimal:
        return self.remaining_quantity * self.cost_basis_per_unit

    @property
    def holding_start(self) -> date:
        return self.holding_period_start or self.acquisition_date

    @property
    def holding_period_days(self) -> int:
        return (date.today() - self.holding_start).days

    def is_long_term_on(self, as_of: date) -> bool:
        """Held more than one year as of ``as_of`` (IRC §1222): long-term only
        when the disposal date is after the one-year anniversary of the start
        of the holding period. A Feb 29 start's anniversary is Feb 28."""
        start = self.holding_start
        try:
            anniversary = start.replace(year=start.year + 1)
        except ValueError:                      # Feb 29 → Feb 28 next year
            anniversary = start.replace(year=start.year + 1, day=28)
        return as_of > anniversary

    @property
    def is_long_term(self) -> bool:
        return self.is_long_term_on(date.today())


class TaxLotDisposal(Base):
    """Links a sale transaction to the specific tax lots it consumed."""

    __tablename__ = "tax_lot_disposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    tax_lot_id: Mapped[int] = mapped_column(ForeignKey("tax_lots.id"), index=True)
    sale_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    quantity_disposed: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    proceeds_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    realized_gain_loss: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    is_short_term: Mapped[bool] = mapped_column(Boolean)
    disposal_date: Mapped[date] = mapped_column(Date)
    is_wash_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    wash_sale_disallowed: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))


class WashSaleAdjustment(Base, TimestampMixin):
    """Audit trail for wash-sale rule basis adjustments."""

    __tablename__ = "wash_sale_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    disallowed_disposal_id: Mapped[int] = mapped_column(ForeignKey("tax_lot_disposals.id"))
    replacement_lot_id: Mapped[int] = mapped_column(ForeignKey("tax_lots.id"))
    disallowed_loss: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    # Replacement shares matched against the loss shares (None on legacy rows,
    # which matched the whole replacement lot).
    matched_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    adjustment_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

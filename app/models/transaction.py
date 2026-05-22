"""Transaction model for the Family Office."""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TransactionTypeEnum(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    FEE = "fee"
    DISTRIBUTION = "distribution"
    CONTRIBUTION = "contribution"
    GIFT_IN = "gift_in"
    GIFT_OUT = "gift_out"
    CORPORATE_ACTION = "corporate_action"
    RETURN_OF_CAPITAL = "return_of_capital"


class Transaction(Base, TimestampMixin):
    """A single financial transaction in an account."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    transaction_type: Mapped[TransactionTypeEnum] = mapped_column(String(50))
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    price_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id"), nullable=True
    )
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

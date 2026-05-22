"""Double-entry bookkeeping models for the Family Office."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ChartOfAccounts(Base):
    """Hierarchical chart of accounts for Family Office bookkeeping."""

    __tablename__ = "chart_of_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    account_type: Mapped[str] = mapped_column(String(50))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_deductible: Mapped[bool] = mapped_column(Boolean, default=False)


class JournalEntry(Base, TimestampMixin):
    """A balanced double-entry journal entry."""

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_posted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_reversing: Mapped[bool] = mapped_column(Boolean, default=False)
    reversed_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(50), default="manual")
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id"), nullable=True
    )

    # Only relationship kept: lines within the same module
    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="journal_entry", cascade="all, delete-orphan"
    )

    @property
    def is_balanced(self) -> bool:
        total_debit = sum(line.debit for line in self.lines)
        total_credit = sum(line.credit for line in self.lines)
        return abs(total_debit - total_credit) < Decimal("0.01")


class JournalLine(Base):
    """A single debit or credit line within a journal entry."""

    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id"), index=True
    )
    coa_id: Mapped[int] = mapped_column(ForeignKey("chart_of_accounts.id"))
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )

    journal_entry: Mapped["JournalEntry"] = relationship(back_populates="lines")

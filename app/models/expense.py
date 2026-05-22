"""Expense tracking models for the Family Office."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ExpenseCategory(Base):
    """Categories for Family Office expenses with deductibility flags."""

    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_deductible: Mapped[bool] = mapped_column(Boolean, default=True)
    deduction_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Vendor(Base, TimestampMixin):
    """A service provider or vendor paid by the Family Office."""

    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    tax_id_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requires_1099: Mapped[bool] = mapped_column(Boolean, default=False)
    vendor_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_info: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Expense(Base, TimestampMixin):
    """A specific expense incurred by the Family Office."""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    receipt_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_year: Mapped[int | None] = mapped_column(nullable=True)

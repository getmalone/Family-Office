"""Account models for the Family Office."""

import enum
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AccountTypeEnum(str, enum.Enum):
    BROKERAGE = "brokerage"
    IRA_TRADITIONAL = "ira_traditional"
    IRA_ROTH = "ira_roth"
    FOUR01K = "401k"
    TRUST = "trust"
    BANK_CHECKING = "bank_checking"
    BANK_SAVINGS = "bank_savings"
    REAL_ESTATE = "real_estate"
    PRIVATE_EQUITY = "private_equity"
    CRYPTO = "crypto"
    HSA = "hsa"
    FIVE29 = "529"
    OTHER = "other"


class Account(Base, TimestampMixin):
    """A financial account held at an institution."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    account_type: Mapped[AccountTypeEnum] = mapped_column(String(50))
    institution: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_number_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    is_taxable: Mapped[bool] = mapped_column(Boolean, default=True)
    tax_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    primary_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id"), nullable=True
    )
    opened_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

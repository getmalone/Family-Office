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


# Map each account type to a retirement-withdrawal tax bucket. These three
# buckets drive the Monte Carlo Income Bridge (withdrawal sequencing):
#   taxable     — already-taxed money; only capital-gains drag on growth
#   traditional — pre-tax / tax-deferred; ordinary income tax on withdrawal
#   roth        — tax-free in retirement (Roth, HSA-qualified, 529-education)
TAX_BUCKETS: tuple[str, str, str] = ("taxable", "traditional", "roth")

_TAX_DEFERRED_TYPES = frozenset({
    AccountTypeEnum.IRA_TRADITIONAL,
    AccountTypeEnum.FOUR01K,
})
_TAX_FREE_TYPES = frozenset({
    AccountTypeEnum.IRA_ROTH,
    AccountTypeEnum.HSA,
    AccountTypeEnum.FIVE29,
})


def tax_bucket_for(account_type: "AccountTypeEnum | str") -> str:
    """Return the withdrawal-sequencing bucket for an account type.

    Accepts the enum or its raw string value (the column is stored as a
    string). Anything not explicitly tax-deferred or tax-free is taxable.
    """
    try:
        at = account_type if isinstance(account_type, AccountTypeEnum) else AccountTypeEnum(account_type)
    except ValueError:
        return "taxable"
    if at in _TAX_DEFERRED_TYPES:
        return "traditional"
    if at in _TAX_FREE_TYPES:
        return "roth"
    return "taxable"


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

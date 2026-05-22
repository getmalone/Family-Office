"""
Family member and entity models.

Tracks natural persons (Principal, spouse, children) and legal entities
(LLCs, trusts, GRATs, foundations) that form the Family Office structure.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class FamilyMember(Base, TimestampMixin):
    """A natural person in the family."""

    __tablename__ = "family_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    ssn_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    relationship: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class FamilyEntity(Base, TimestampMixin):
    """A legal entity in the Family Office structure."""

    __tablename__ = "family_entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    entity_type: Mapped[str] = mapped_column(String(50))
    tax_id_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    formation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tax_election: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Ownership(Base, TimestampMixin):
    """Tracks ownership percentages between family members/entities and accounts."""

    __tablename__ = "ownership"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_member_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id"), nullable=True
    )
    owner_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    ownership_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("100"))
    effective_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

"""Estate planning and family governance models for the Family Office."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Gift(Base, TimestampMixin):
    """Records a gift made by or to a family member."""

    __tablename__ = "gifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    donor_member_id: Mapped[int] = mapped_column(ForeignKey("family_members.id"))
    recipient_member_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id"), nullable=True
    )
    recipient_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    recipient_external: Mapped[str | None] = mapped_column(String(300), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    gift_date: Mapped[date] = mapped_column(Date)
    fair_market_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    donor_cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    gift_tax_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    annual_exclusion_applied: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    lifetime_exemption_applied: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    tax_year: Mapped[int] = mapped_column()
    form_709_filed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_charitable: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class GRATSimulation(Base, TimestampMixin):
    """Grantor Retained Annuity Trust (GRAT) simulation."""

    __tablename__ = "grat_simulations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    grantor_member_id: Mapped[int] = mapped_column(ForeignKey("family_members.id"))
    initial_funding: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    term_years: Mapped[int] = mapped_column()
    section_7520_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    assumed_growth_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    annuity_payment: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    remainder_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    tax_savings_estimate: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    simulation_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft")

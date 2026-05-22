"""Estate planning Pydantic schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class GiftCreate(BaseModel):
    donor_member_id: int
    recipient_member_id: int | None = None
    recipient_entity_id: int | None = None
    recipient_external: str | None = None
    asset_id: int | None = None
    gift_date: date
    fair_market_value: Decimal
    donor_cost_basis: Decimal | None = None
    is_charitable: bool = False
    notes: str | None = None


class GiftOut(BaseModel):
    id: int
    donor_name: str
    recipient_name: str
    gift_date: date
    fair_market_value: Decimal
    annual_exclusion_applied: Decimal
    lifetime_exemption_applied: Decimal
    is_charitable: bool


class GRATParams(BaseModel):
    name: str
    grantor_member_id: int
    initial_funding: Decimal
    term_years: int = 2
    section_7520_rate: Decimal = Decimal("5.0")
    assumed_growth_rates: list[Decimal] = [Decimal("4"), Decimal("6"), Decimal("8"), Decimal("10")]


class GRATResult(BaseModel):
    growth_rate: Decimal
    annuity_payment: Decimal
    remainder_value: Decimal
    tax_savings_estimate: Decimal
    effective_transfer_pct: Decimal


class OwnershipNode(BaseModel):
    id: int
    name: str
    node_type: str  # member or entity
    ownership_pct: Decimal
    children: list["OwnershipNode"] = []

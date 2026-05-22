"""Tax optimization Pydantic schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class HarvestCandidate(BaseModel):
    asset_id: int
    symbol: str | None
    name: str
    account_id: int
    account_name: str
    total_unrealized_loss: Decimal
    num_lots: int
    oldest_lot_date: date
    is_short_term: bool
    wash_sale_risk: bool
    estimated_tax_savings: Decimal
    substitute_symbols: list[str] = []


class TaxSummary(BaseModel):
    tax_year: int
    short_term_gains: Decimal
    short_term_losses: Decimal
    long_term_gains: Decimal
    long_term_losses: Decimal
    net_gain_loss: Decimal
    dividends_qualified: Decimal
    dividends_ordinary: Decimal
    interest_income: Decimal
    deductible_expenses: Decimal
    estimated_tax_liability: Decimal
    loss_carryforward: Decimal


class ElectionSimResult(BaseModel):
    election_type: str  # e.g. "section_475", "specific_id_vs_fifo"
    scenario_name: str
    estimated_tax_impact: Decimal
    description: str
    recommended: bool = False

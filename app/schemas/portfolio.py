"""Portfolio-related Pydantic schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class HoldingDetail(BaseModel):
    asset_id: int
    symbol: str | None
    name: str
    asset_class: str
    account_id: int
    account_name: str
    quantity: Decimal
    cost_basis: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_gain_loss: Decimal
    unrealized_pct: Decimal
    day_change: Decimal = Decimal("0")
    weight_pct: Decimal = Decimal("0")


class PortfolioSummary(BaseModel):
    total_market_value: Decimal
    total_cost_basis: Decimal
    total_unrealized_gain_loss: Decimal
    total_realized_ytd: Decimal
    day_change: Decimal
    holdings: list[HoldingDetail]
    allocation: dict[str, Decimal]  # asset_class -> pct


class TradeRequest(BaseModel):
    account_id: int
    asset_symbol: str
    action: str  # buy or sell
    quantity: Decimal
    limit_price: Decimal | None = None
    reason: str = ""


class TradeResult(BaseModel):
    transaction_id: int
    status: str  # executed, pending_approval
    approval_id: int | None = None
    summary: str
    requires_approval: bool = False

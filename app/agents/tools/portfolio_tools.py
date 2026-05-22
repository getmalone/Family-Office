"""
Portfolio Agent tools — LangChain tool wrappers around PortfolioService.

These tools enable the Portfolio Agent to continuously monitor markets and
investment opportunities, suggest and execute trades, and maintain a unified
portfolio database with cost basis and tax lot tracking.
"""

from decimal import Decimal

from langchain_core.tools import tool
from sqlalchemy.orm import Session

from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService


def create_portfolio_tools(session_factory):
    """Create portfolio tools with injected session factory."""

    @tool
    def get_portfolio_summary(account_id: int | None = None) -> str:
        """Get current portfolio positions with market values, cost basis, and unrealized gains/losses."""
        with session_factory() as session:
            svc = PortfolioService(session)
            summary = svc.get_summary(account_id=account_id)
            return summary.model_dump_json()

    @tool
    def get_market_quote(symbol: str) -> str:
        """Get the current market price for a stock/ETF by ticker symbol."""
        with session_factory() as session:
            svc = MarketDataService(session)
            price = svc.get_current_price_by_symbol(symbol)
            if price is None:
                return f"No price available for {symbol}"
            return f"{symbol}: ${price}"

    @tool
    def get_open_tax_lots(account_id: int | None = None, asset_id: int | None = None) -> str:
        """Get all open tax lots, optionally filtered by account or asset."""
        with session_factory() as session:
            svc = PortfolioService(session)
            lots = svc.get_open_lots(account_id=account_id, asset_id=asset_id)
            result = []
            for lot in lots:
                result.append({
                    "id": lot.id,
                    "asset_id": lot.asset_id,
                    "account_id": lot.account_id,
                    "acquisition_date": str(lot.acquisition_date),
                    "remaining_quantity": str(lot.remaining_quantity),
                    "cost_basis_per_unit": str(lot.cost_basis_per_unit),
                    "is_long_term": lot.is_long_term,
                    "total_cost_basis": str(lot.total_cost_basis),
                })
            import json
            return json.dumps(result)

    @tool
    def execute_buy_trade(
        account_id: int,
        asset_id: int,
        quantity: float,
        price_per_unit: float,
        notes: str = "",
    ) -> str:
        """Execute a buy trade. Creates a transaction and tax lot."""
        with session_factory() as session:
            svc = PortfolioService(session)
            txn = svc.execute_buy(
                account_id=account_id,
                asset_id=asset_id,
                quantity=Decimal(str(quantity)),
                price_per_unit=Decimal(str(price_per_unit)),
                notes=notes,
            )
            session.commit()
            return f"Buy executed: Transaction #{txn.id}, {quantity} units at ${price_per_unit}"

    @tool
    def execute_sell_trade(
        account_id: int,
        asset_id: int,
        quantity: float,
        price_per_unit: float,
        method: str = "fifo",
        notes: str = "",
    ) -> str:
        """Execute a sell trade with automatic lot selection. Returns realized gain/loss."""
        with session_factory() as session:
            svc = PortfolioService(session)
            txn, disposals = svc.execute_sell(
                account_id=account_id,
                asset_id=asset_id,
                quantity=Decimal(str(quantity)),
                price_per_unit=Decimal(str(price_per_unit)),
                method=method,
                notes=notes,
            )
            total_gain = sum(d.realized_gain_loss for d in disposals)
            session.commit()
            return (
                f"Sell executed: Transaction #{txn.id}, {quantity} units at ${price_per_unit}. "
                f"Realized gain/loss: ${total_gain}"
            )

    return [get_portfolio_summary, get_market_quote, get_open_tax_lots, execute_buy_trade, execute_sell_trade]

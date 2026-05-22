"""Tests for the Portfolio Service."""

from datetime import date
from decimal import Decimal

from app.models.asset import Asset, AssetClassEnum
from app.models.tax_lot import TaxLot
from app.services.portfolio_service import PortfolioService


def test_get_summary(seeded_session):
    """Portfolio summary includes seeded positions."""
    svc = PortfolioService(seeded_session)
    summary = svc.get_summary()

    assert summary.total_market_value > 0
    assert len(summary.holdings) >= 1

    # VOO position: 100 shares at $450 cost, $520.50 current
    voo_holding = next((h for h in summary.holdings if h.symbol == "VOO"), None)
    assert voo_holding is not None
    assert voo_holding.quantity == Decimal("100")
    assert voo_holding.cost_basis == Decimal("45000.00")
    assert voo_holding.unrealized_gain_loss > 0  # $520.50 - $450 = gain


def test_execute_buy(seeded_session):
    """Buy creates a transaction and tax lot."""
    svc = PortfolioService(seeded_session)
    aapl = seeded_session.query(Asset).filter(Asset.symbol == "AAPL").one()
    from app.models.account import Account
    acct = seeded_session.query(Account).first()

    txn = svc.execute_buy(
        account_id=acct.id,
        asset_id=aapl.id,
        quantity=Decimal("50"),
        price_per_unit=Decimal("195.00"),
    )

    assert txn.id is not None
    assert txn.total_amount == Decimal("9750.00")

    # Tax lot should exist
    lots = svc.get_open_lots(asset_id=aapl.id)
    assert len(lots) == 1
    assert lots[0].remaining_quantity == Decimal("50")


def test_execute_sell_fifo(seeded_session):
    """Sell uses FIFO and creates disposals with realized gain/loss."""
    svc = PortfolioService(seeded_session)
    from app.models.account import Account
    acct = seeded_session.query(Account).first()
    voo = seeded_session.query(Asset).filter(Asset.symbol == "VOO").one()

    txn, disposals = svc.execute_sell(
        account_id=acct.id,
        asset_id=voo.id,
        quantity=Decimal("30"),
        price_per_unit=Decimal("520.00"),
    )

    assert txn.id is not None
    assert len(disposals) == 1
    assert disposals[0].quantity_disposed == Decimal("30")
    # Gain: (520 - 450) * 30 = $2,100
    assert disposals[0].realized_gain_loss == Decimal("2100.00")

    # Remaining lot should have 70 units
    lots = svc.get_open_lots(asset_id=voo.id)
    assert lots[0].remaining_quantity == Decimal("70")

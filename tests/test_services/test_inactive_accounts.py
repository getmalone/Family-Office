"""Removed (soft-deleted) accounts must vanish from every current-value view.

The bug: deactivating an account removed it from the Accounts page, but its
open lots still counted in the dashboard AUM, holdings, day-change, snapshots,
the pricing universe, and tax tooling — a ~$400k phantom. One definition of
"currently held" now lives in app/services/holdings.py; these tests pin each
consumer to it, and pin what deliberately does NOT change (realized history).
"""

from datetime import date, timedelta
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot, TaxLotDisposal
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService
from app.services.snapshot_service import SnapshotService
from app.services.symbol_service import SymbolService
from app.services.tax_service import TaxService


def _account(session, aid, name):
    a = Account(id=aid, name=name, account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add(a); session.flush()
    return a


def _position(session, account, asset_id, symbol, qty="10", cost="100", price="150"):
    asset = session.get(Asset, asset_id) or Asset(
        id=asset_id, symbol=symbol, name=f"{symbol} pos",
        asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(asset); session.flush()
    session.merge(AssetPrice(asset_id=asset_id, price_date=date.today(),
                             close_price=Decimal(price), source="test"))
    txn = Transaction(account_id=account.id, asset_id=asset_id,
                      transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2024, 1, 2), quantity=Decimal(qty),
                      price_per_unit=Decimal(cost),
                      total_amount=Decimal(qty) * Decimal(cost), fees=Decimal("0"))
    session.add(txn); session.flush()
    lot = TaxLot(account_id=account.id, asset_id=asset_id,
                 acquisition_date=date(2024, 1, 2), acquisition_transaction_id=txn.id,
                 original_quantity=Decimal(qty), remaining_quantity=Decimal(qty),
                 cost_basis_per_unit=Decimal(cost), original_cost_basis_per_unit=Decimal(cost))
    session.add(lot); session.flush()
    return lot


def _two_account_book(session):
    """Active account A (VOO), account B (AAPL) that tests deactivate."""
    a = _account(session, 1, "Keep")
    b = _account(session, 2, "Removed")
    _position(session, a, 1, "VOO")                      # 10 × $150 = $1500
    _position(session, b, 2, "AAPL", price="200")        # 10 × $200 = $2000
    session.flush()
    return a, b


def test_summary_drops_inactive_account_aum_and_holdings(session):
    a, b = _two_account_book(session)
    svc = PortfolioService(session)

    before = svc.get_summary()
    assert before.total_market_value == Decimal("3500")

    b.is_active = False
    session.flush()
    after = svc.get_summary()
    assert after.total_market_value == Decimal("1500")   # phantom $2000 gone
    assert [h.symbol for h in after.holdings] == ["VOO"]
    assert all(h.account_id != b.id for h in after.holdings)


def test_realized_history_survives_account_removal(session):
    a, b = _two_account_book(session)
    lot_b = session.query(TaxLot).filter_by(account_id=b.id).first()
    sale = Transaction(account_id=b.id, asset_id=lot_b.asset_id,
                       transaction_type=TransactionTypeEnum.SELL,
                       transaction_date=date.today() - timedelta(days=30),
                       quantity=Decimal("5"), price_per_unit=Decimal("180"),
                       total_amount=Decimal("900"), fees=Decimal("0"))
    session.add(sale); session.flush()
    session.add(TaxLotDisposal(
        tax_lot_id=lot_b.id, sale_transaction_id=sale.id,
        disposal_date=date.today() - timedelta(days=30),
        quantity_disposed=Decimal("5"), proceeds_per_unit=Decimal("180"),
        realized_gain_loss=Decimal("400"), is_short_term=True))
    session.flush()

    b.is_active = False
    session.flush()
    summary = PortfolioService(session).get_summary()
    # Gains already realized this year are tax events — they must not vanish.
    assert summary.total_realized_ytd == Decimal("400")


def test_held_universe_excludes_inactive_everywhere(session):
    a, b = _two_account_book(session)
    b.is_active = False
    session.flush()

    md_assets = {x.symbol for x in MarketDataService(session)._held_public_assets()}
    sym_assets = {x.symbol for x in SymbolService(session)._held_public_assets()}
    snap_qty = SnapshotService(session)._qty_by_asset()

    assert md_assets == {"VOO"}                          # backfill/refresh universe
    assert sym_assets == {"VOO"}                         # resolver universe
    assert set(snap_qty) == {1}                          # snapshot day-change quantities


def test_brief_portfolio_impact_excludes_inactive(session):
    from app.services.morning_brief_service import MorningBriefService
    a, b = _two_account_book(session)
    b.is_active = False
    session.flush()

    rows, totals = MorningBriefService(session)._portfolio_day_impact(
        date.today(), date.today() - timedelta(days=1))
    assert {r["symbol"] for r in rows} == {"VOO"}
    assert totals["total_mv"] == 1500.0


def test_harvest_candidates_exclude_inactive(session):
    a, b = _two_account_book(session)
    # Underwater position in each account (cost 100, price 50).
    _position(session, a, 3, "LOSA", cost="100", price="50")
    _position(session, b, 4, "LOSB", cost="100", price="50")
    b.is_active = False
    session.flush()

    cands = TaxService(session).find_harvest_candidates()
    symbols = {c.symbol for c in cands}
    assert "LOSA" in symbols
    assert "LOSB" not in symbols                         # can't harvest in a removed account


def test_open_lots_and_trends_exclude_inactive(session):
    a, b = _two_account_book(session)
    b.is_active = False
    session.flush()
    svc = PortfolioService(session)

    assert {l.account_id for l in svc.get_open_lots()} == {a.id}
    hist = svc.get_portfolio_history(days=30)
    assert "Removed" not in hist["by_account"]

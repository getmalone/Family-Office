"""The background backfill that populates the trailing-window Performance panel.

Renders never go to the network; instead this worker pulls ~2 years of price
history out-of-band. These lock in: it fetches ONLY inside an allow_network()
block, only when history is thin, and it busts the period-returns cache so the
panel refreshes once the history lands.
"""

from datetime import date
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services import analysis_service, backfill_worker
from app.services.market_data import MarketDataService
from app.services.render_guard import network_allowed


def _seed_public_holding(session, symbol="AAA"):
    a = Asset(id=1, symbol=symbol, name=symbol + " Co",
              asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    acct = Account(id=1, name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([a, acct]); session.flush()
    txn = Transaction(account_id=1, asset_id=1, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2024, 1, 1), quantity=Decimal("10"),
                      price_per_unit=Decimal("50"), total_amount=Decimal("500"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=1, asset_id=1, acquisition_date=date(2024, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal("10"),
                       remaining_quantity=Decimal("10"), cost_basis_per_unit=Decimal("50"),
                       original_cost_basis_per_unit=Decimal("50")))
    session.flush()
    return a


def test_run_backfill_fetches_under_network_guard(session, monkeypatch):
    _seed_public_holding(session)

    captured = {}

    def fake_thin(self, min_dates=200):
        return True  # force a fetch

    def fake_backfill(self, months=24):
        captured["network_allowed"] = network_allowed()  # must be inside allow_network()
        return {"symbols": 1, "rows_added": 123}

    monkeypatch.setattr(MarketDataService, "history_is_thin", fake_thin)
    monkeypatch.setattr(MarketDataService, "backfill_history", fake_backfill)
    monkeypatch.setattr(MarketDataService, "refresh_proxy_prices", lambda self: 0)

    analysis_service._PERIOD_RETURNS_CACHE["stale"] = [{"old": True}]
    result = backfill_worker.run_backfill_now(session=session)

    assert result == {"symbols": 1, "rows_added": 123}
    assert captured["network_allowed"] is True            # fetched with the guard lifted
    assert network_allowed() is False                     # ...and restored afterwards
    assert analysis_service._PERIOD_RETURNS_CACHE == {}    # cache busted so panel recomputes


def test_run_backfill_skips_when_history_already_deep(session, monkeypatch):
    _seed_public_holding(session)
    monkeypatch.setattr(MarketDataService, "history_is_thin", lambda self, min_dates=200: False)

    def _boom(self, months=24):
        raise AssertionError("must not fetch when history is already deep")

    monkeypatch.setattr(MarketDataService, "backfill_history", _boom)
    assert backfill_worker.run_backfill_now(session=session) == {"skipped": "history already deep"}


def test_ensure_history_guards_against_double_start(monkeypatch):
    # Pretend a job is already running → ensure_history must not start another.
    monkeypatch.setattr(backfill_worker, "_running", True, raising=False)
    assert backfill_worker.ensure_history() is False

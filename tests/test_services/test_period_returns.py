"""Tests for trailing-window (3M/6M/12M/YTD) portfolio returns on the dashboard."""

from datetime import date
from decimal import Decimal

import numpy as np

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.analysis_service import AnalysisService, _PERIOD_RETURNS_CACHE


def _hold(session, aid, sym, cls, qty, price):
    a = Asset(id=aid, symbol=sym, name=sym, asset_class=cls, is_publicly_traded=True)
    acct = session.query(Account).first() or Account(id=1, name="B", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([a, acct]); session.flush()
    txn = Transaction(account_id=acct.id, asset_id=aid, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2022, 1, 1), quantity=Decimal(qty), price_per_unit=Decimal(price),
                      total_amount=Decimal(qty) * Decimal(price), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=acct.id, asset_id=aid, acquisition_date=date(2022, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal(qty),
                       remaining_quantity=Decimal(qty), cost_basis_per_unit=Decimal(price),
                       original_cost_basis_per_unit=Decimal(price)))
    session.add(AssetPrice(asset_id=aid, price_date=date.today(), close_price=Decimal(price), source="test"))
    session.flush()


def test_period_returns_basic(session, monkeypatch):
    _PERIOD_RETURNS_CACHE.clear()
    _hold(session, 1, "VOO", AssetClassEnum.US_EQUITY, "100", "100")   # MV = $10,000
    rets = np.full(300, 0.001)                                         # +0.1%/day, 300 days
    monkeypatch.setattr(AnalysisService, "_bulk_daily_returns", lambda self, sy, s, e: {"VOO": rets})

    out = {r["key"]: r for r in AnalysisService(session).get_period_returns()}
    assert all(out[k]["available"] for k in ("3M", "6M", "12M", "YTD"))

    cum_12m = float(np.prod(1 + rets[-252:])) - 1                      # ≈ 28.7%
    assert out["12M"]["pct"] == round(cum_12m * 100, 2)
    # Dollar gain is exact: end (10,000) − start (end / (1+cum)).
    assert out["12M"]["dollar"] == round(10000 - 10000 / (1 + cum_12m), 2)
    assert out["12M"]["up"] is True
    # Longer window → larger cumulative gain.
    assert out["12M"]["pct"] > out["3M"]["pct"] > 0


def test_cash_is_held_flat(session, monkeypatch):
    _PERIOD_RETURNS_CACHE.clear()
    _hold(session, 1, "VOO", AssetClassEnum.US_EQUITY, "100", "100")    # $10,000 equity
    _hold(session, 2, "CASH", AssetClassEnum.CASH, "90000", "1")        # $90,000 cash, flat
    rets = np.full(300, 0.001)
    # CASH must be excluded from the symbols fetched (pinned flat, not yfinanced).
    def fake(self, symbols, s, e):
        assert "CASH" not in symbols
        return {"VOO": rets}
    monkeypatch.setattr(AnalysisService, "_bulk_daily_returns", fake)

    out = {r["key"]: r for r in AnalysisService(session).get_period_returns()}
    voo_12m = float(np.prod(1 + rets[-252:])) - 1
    # Portfolio is 90% flat cash → its % return is far below VOO's standalone return.
    assert 0 < out["12M"]["pct"] < voo_12m * 100 * 0.2


def test_insufficient_history_marks_unavailable(session, monkeypatch):
    _PERIOD_RETURNS_CACHE.clear()
    _hold(session, 1, "VOO", AssetClassEnum.US_EQUITY, "100", "100")
    monkeypatch.setattr(AnalysisService, "_bulk_daily_returns", lambda self, sy, s, e: {"VOO": np.full(5, 0.001)})
    out = {r["key"]: r for r in AnalysisService(session).get_period_returns()}
    assert not any(out[k]["available"] for k in ("3M", "6M", "12M"))    # 5 days < every window


def test_empty_portfolio_returns_empty(session):
    _PERIOD_RETURNS_CACHE.clear()
    assert AnalysisService(session).get_period_returns() == []

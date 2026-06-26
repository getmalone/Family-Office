"""Page renders must never touch the network.

A synchronous yfinance call during render is what turned page loads into 20–40s
hangs (and dumped the PWA to its offline screen). These lock in the rule: during
an ordinary render the market-data fetchers stay DB-only; the network is reached
ONLY inside an explicit ``allow_network()`` block (the Refresh / backfill paths).
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.analysis_service import AnalysisService
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService
from app.services.render_guard import allow_network, network_allowed


def _boom(*args, **kwargs):
    raise AssertionError("a render must not hit the network")


def _seed_holding(session, symbol, qty="10", price="50", publicly_traded=True):
    a = Asset(id=1, symbol=symbol, name=symbol + " Co",
              asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=publicly_traded)
    acct = Account(id=1, name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([a, acct]); session.flush()
    txn = Transaction(account_id=1, asset_id=1, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2024, 1, 1), quantity=Decimal(qty),
                      price_per_unit=Decimal(price), total_amount=Decimal(qty) * Decimal(price),
                      fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=1, asset_id=1, acquisition_date=date(2024, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal(qty),
                       remaining_quantity=Decimal(qty), cost_basis_per_unit=Decimal(price),
                       original_cost_basis_per_unit=Decimal(price)))
    session.flush()
    return a


def test_render_guard_default_off_and_scoped():
    assert network_allowed() is False
    with allow_network():
        assert network_allowed() is True
    assert network_allowed() is False  # always restored, even via the ctx manager


def test_get_current_price_no_network_on_render(session, monkeypatch):
    a = Asset(symbol="ZZZZ", name="No History Co",
              asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(a); session.flush()
    monkeypatch.setattr(MarketDataService, "_fetch_and_cache", _boom)

    # Render path: an unpriced asset returns None, never the network.
    assert MarketDataService(session).get_current_price(a) is None
    # Explicit-refresh path: the same call is now allowed to fetch.
    with allow_network():
        with pytest.raises(AssertionError):
            MarketDataService(session).get_current_price(a)


def test_get_summary_unpriced_equity_does_not_block(session, monkeypatch):
    _seed_holding(session, "ZZZZ")  # no AssetPrice rows at all
    monkeypatch.setattr(MarketDataService, "_fetch_and_cache", _boom)
    summary = PortfolioService(session).get_summary()  # must not raise / not fetch
    assert summary is not None
    assert summary.holdings[0].symbol == "ZZZZ"


def test_bulk_daily_returns_no_network_on_render(session, monkeypatch):
    # A symbol with no stored history must not trigger a download during render.
    import yfinance as yf
    monkeypatch.setattr(yf, "download", _boom)
    svc = AnalysisService(session)
    out = svc._bulk_daily_returns(["NOPE"], date(2024, 1, 1), date(2024, 6, 1))
    assert out == {} or "NOPE" not in out  # degrades to "no data", no exception


def test_fund_holdings_no_network_on_render(session, monkeypatch):
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", _boom)
    svc = AnalysisService(session)
    assert svc._get_fund_holdings(["SPY", "QQQ"]) == {}  # no per-fund Ticker calls on render


def test_morning_brief_builds_without_live_regime_download(session, monkeypatch):
    """The brief must build from stored data — never the old per-holding 2-year
    download (the 20–40s culprit)."""
    import pandas as pd
    import yfinance as yf
    from app.services.markov_regime_service import MarkovRegimeService
    from app.services.morning_brief_service import MorningBriefService, invalidate_brief_cache

    _seed_holding(session, "AAA")
    session.add(AssetPrice(asset_id=1, price_date=date.today(), close_price=Decimal("50"), source="yfinance"))
    session.flush()

    # The live-download regime path is forbidden; the bounded market snapshot is
    # allowed to "succeed" with empty data (no real network).
    monkeypatch.setattr(MarkovRegimeService, "get_portfolio_and_ticker_regimes", _boom)
    monkeypatch.setattr(yf, "download", lambda *a, **k: pd.DataFrame())

    invalidate_brief_cache()
    brief = MorningBriefService(session).get_brief()
    invalidate_brief_cache()
    assert isinstance(brief, dict)
    assert "holdings_impact" in brief

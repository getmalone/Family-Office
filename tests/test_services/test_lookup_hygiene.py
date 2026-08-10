"""Lookup hygiene — the fixes behind the v0.1.16 "lookups failing" triage.

Covers the three failure classes from the field error log:
  • raw class-share symbols (``BRK/B``) sent to Yahoo → mangled URL, 502/404
  • CUSIP symbols (``14022L645``) sent to Yahoo → guaranteed 404 per refresh
  • Monte Carlo re-downloading 2y of history per render (rate-limit storm)
plus the morning brief blanking its market section for a whole session when one
batch gets rate-limited.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.markov_regime_service import MarkovRegimeService, WINDOW
from app.services.render_guard import allow_network


def _hold(session, aid, symbol):
    a = Asset(id=aid, symbol=symbol, name=f"{symbol} pos", asset_class=AssetClassEnum.US_EQUITY,
              is_publicly_traded=True)
    acct = session.query(Account).first() or Account(
        id=1, name="B", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([a, acct]); session.flush()
    txn = Transaction(account_id=acct.id, asset_id=aid, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2024, 1, 1), quantity=Decimal("10"),
                      price_per_unit=Decimal("100"), total_amount=Decimal("1000"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=acct.id, asset_id=aid, acquisition_date=date(2024, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal("10"),
                       remaining_quantity=Decimal("10"), cost_basis_per_unit=Decimal("100"),
                       original_cost_basis_per_unit=Decimal("100")))
    session.flush()
    return a


def _multi_close_frame(ticker_cols: list[str], rows: int = 120) -> pd.DataFrame:
    """A yf.download-shaped multi-ticker frame: MultiIndex (field, ticker)."""
    idx = pd.date_range(end=date.today(), periods=rows, freq="D")
    data = {("Close", t): np.linspace(100, 140, rows) for t in ticker_cols}
    return pd.DataFrame(data, index=idx)


# ── Markov service boundary ──────────────────────────────────────────────────

def test_regime_batch_normalises_and_maps_back():
    captured = {}

    def fake_download(symbols, **kw):
        captured["symbols"] = sorted(symbols)
        return _multi_close_frame(symbols)

    with patch("yfinance.download", side_effect=fake_download):
        port, per = MarkovRegimeService().get_portfolio_and_ticker_regimes(
            symbols=["BRK/B", "14022L645", "SPY"],
            weights={"BRK/B": 0.5, "SPY": 0.5},
        )

    # Only Yahoo-resolvable forms went over the wire — no slash, no CUSIP.
    assert captured["symbols"] == ["BRK-B", "SPY"]
    # Results keyed by the caller's original symbols.
    assert per["BRK/B"] is not None and per["BRK/B"].symbol == "BRK/B"
    assert per["SPY"] is not None
    assert per["14022L645"] is None
    assert port is not None                     # blended from the two priced legs


def test_regime_batch_all_junk_never_touches_network():
    with patch("yfinance.download", side_effect=AssertionError("network hit")):
        port, per = MarkovRegimeService().get_portfolio_and_ticker_regimes(
            symbols=["14022L645", "31617E745"], weights={},
        )
    assert port is None
    assert per == {"14022L645": None, "31617E745": None}


def test_fetch_prices_uses_yahoo_form_and_skips_cusip():
    seen = {}

    class FakeTicker:
        def __init__(self, sym):
            seen["sym"] = sym
        def history(self, period):
            idx = pd.date_range(end=date.today(), periods=WINDOW + 20, freq="D")
            return pd.DataFrame({"Close": np.linspace(1, 2, WINDOW + 20)}, index=idx)

    with patch("yfinance.Ticker", FakeTicker):
        prices = MarkovRegimeService()._fetch_prices("BRK/B", 504)
    assert seen["sym"] == "BRK-B"
    assert prices is not None and len(prices) == WINDOW + 20

    with patch("yfinance.Ticker", side_effect=AssertionError("network hit")):
        assert MarkovRegimeService()._fetch_prices("14022L645", 504) is None


# ── Monte Carlo regime: stored history only ──────────────────────────────────

def test_mc_regime_reads_db_never_downloads(session):
    a = _hold(session, 1, "VOO")
    _hold(session, 2, "14022L645")              # CUSIP position must not break it
    start = date.today() - timedelta(days=600)
    for i in range(0, 600, 1):
        session.add(AssetPrice(asset_id=a.id, price_date=start + timedelta(days=i),
                               close_price=Decimal(str(100 + i * 0.1)), source="t"))
    session.flush()

    from app.services.analysis_service import AnalysisService
    summary = SimpleNamespace(holdings=[
        SimpleNamespace(symbol="VOO", market_value=Decimal("9000")),
        SimpleNamespace(symbol="14022L645", market_value=Decimal("1000")),
    ])

    with patch("yfinance.download", side_effect=AssertionError("MC render hit the network")):
        regime = AnalysisService(session)._resolve_portfolio_regime(
            summary, initial_value=10000.0, regime_override=None)

    assert regime is not None                   # computed purely from stored rows
    assert regime.current_regime in {"Bull", "Sideways", "Bear"}


def test_mc_regime_thin_history_returns_none_not_network(session):
    _hold(session, 1, "VOO")                    # held but no stored prices yet
    from app.services.analysis_service import AnalysisService
    summary = SimpleNamespace(holdings=[SimpleNamespace(symbol="VOO", market_value=Decimal("100"))])
    with patch("yfinance.download", side_effect=AssertionError("MC render hit the network")):
        regime = AnalysisService(session)._resolve_portfolio_regime(
            summary, initial_value=100.0, regime_override=None)
    assert regime is None                       # unconditioned sim, no fetch


# ── Bulk daily returns: normalised fetch, junk skipped ───────────────────────

def test_bulk_daily_returns_normalises_and_skips_junk(session):
    from app.services.analysis_service import AnalysisService
    captured = {}

    def fake_download(symbols, **kw):
        captured["symbols"] = sorted(symbols)
        idx = pd.date_range(end=date.today(), periods=15, freq="D")
        return pd.DataFrame({"Close": np.linspace(100, 110, 15)}, index=idx)

    svc = AnalysisService(session)
    with allow_network(), patch("yfinance.download", side_effect=fake_download):
        res = svc._bulk_daily_returns(
            ["BRK/B", "14022L645"], date.today() - timedelta(days=30), date.today())

    assert captured["symbols"] == ["BRK-B"]     # CUSIP never fetched
    assert "BRK/B" in res and len(res["BRK/B"]) == 14
    assert "14022L645" not in res


def test_fund_holdings_skips_cusip_without_network(session):
    from app.services.analysis_service import AnalysisService
    svc = AnalysisService(session)
    with allow_network(), patch("yfinance.Ticker", side_effect=AssertionError("network hit")):
        assert svc._get_fund_holdings(["14022L645"]) == {}


# ── Morning brief: last-good market fallback ─────────────────────────────────

@pytest.fixture
def brief_state():
    """Isolate the brief module's global cache/fallback state per test."""
    import app.services.morning_brief_service as mbs
    mbs._brief_cache.clear()
    mbs._last_good_market.clear()
    mbs._last_good_at = None
    yield mbs
    mbs._brief_cache.clear()
    mbs._last_good_market.clear()
    mbs._last_good_at = None


def test_brief_outage_with_no_history_says_unavailable(session, brief_state):
    mbs = brief_state
    svc = mbs.MorningBriefService(session)
    with patch.object(mbs.MorningBriefService, "_fetch_market_snapshots", return_value={}):
        brief = svc._build_brief(datetime.now(mbs.ET))
    assert brief["indices"] == [] and brief["sectors"] == []
    assert "unavailable" in brief["market_note"]


def test_brief_outage_falls_back_to_last_good(session, brief_state):
    mbs = brief_state
    good = {s: {"price": 100.0, "prev_price": 99.0, "chg_abs": 1.0, "chg_pct": 1.01}
            for s in list(mbs.INDICES) + list(mbs.SECTORS)}
    svc = mbs.MorningBriefService(session)

    # First build: full fetch succeeds and seeds the fallback store.
    with patch.object(mbs.MorningBriefService, "_fetch_market_snapshots", return_value=good):
        first = svc._build_brief(datetime.now(mbs.ET))
    assert first["market_note"] is None
    assert len(first["indices"]) == len(mbs.INDICES)

    # Second build: Yahoo rate-limits everything → rows survive, note appears.
    with patch.object(mbs.MorningBriefService, "_fetch_market_snapshots", return_value={}):
        second = svc._build_brief(datetime.now(mbs.ET))
    assert len(second["indices"]) == len(mbs.INDICES)
    assert len(second["sectors"]) == len(mbs.SECTORS)
    assert "last available" in second["market_note"]


def test_brief_partial_outage_fills_gaps_only(session, brief_state):
    mbs = brief_state
    full = {s: {"price": 100.0, "prev_price": 99.0, "chg_abs": 1.0, "chg_pct": 1.01}
            for s in list(mbs.INDICES) + list(mbs.SECTORS)}
    partial = {k: dict(v, price=200.0) for k, v in full.items() if k not in ("SPY", "QQQ")}
    svc = mbs.MorningBriefService(session)

    with patch.object(mbs.MorningBriefService, "_fetch_market_snapshots", return_value=full):
        svc._build_brief(datetime.now(mbs.ET))
    with patch.object(mbs.MorningBriefService, "_fetch_market_snapshots", return_value=partial):
        brief = svc._build_brief(datetime.now(mbs.ET))

    by_sym = {r["symbol"]: r for r in brief["indices"]}
    assert by_sym["SPY"]["price"] == 100.0      # gap filled from last good
    assert by_sym["DIA"]["price"] == 200.0      # fresh value kept
    assert "couldn't refresh" in brief["market_note"]

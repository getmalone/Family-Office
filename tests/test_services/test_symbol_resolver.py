"""Ticker-resolver pipeline (v0.1.17): verify-first, OpenFIGI, Yahoo name
search with confidence scoring, one-click suggestions, and terminal
"no_listing" for unlisted instruments (CITs)."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.render_guard import allow_network
from app.services.symbol_service import SymbolService, name_match_score


def _hold(session, aid, symbol, name):
    a = Asset(id=aid, symbol=symbol, name=name, asset_class=AssetClassEnum.US_EQUITY,
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


def _stub(monkeypatch, *, figi=None, search=None, prices_ok=()):
    """Stub the three network backends of SymbolService."""
    ok = set(prices_ok)
    monkeypatch.setattr(SymbolService, "_openfigi_map", staticmethod(lambda c: figi or {}))
    monkeypatch.setattr(SymbolService, "_yahoo_search", staticmethod(lambda q: search or []))
    monkeypatch.setattr(SymbolService, "_prices_ok", lambda self, s: s in ok)


def _quote(symbol, name, qt="ETF", exch="PCX"):
    return {"symbol": symbol, "shortname": name, "quoteType": qt, "exchange": exch}


# ── Scoring ──────────────────────────────────────────────────────────────────

def test_name_match_score_bands():
    # Identical fund names → certain match, eligible for auto-apply.
    assert name_match_score("ISHARES CORE S&P 500 ETF", "iShares Core S&P 500 ETF") > 0.99
    # Corporate-suffix noise ignored, share-class words kept: lands in the
    # suggest band, never auto-applied.
    s = name_match_score("Berkshire Hathaway Class B", "Berkshire Hathaway Inc. New")
    assert 0.40 <= s < 0.85
    # Unrelated names score low.
    assert name_match_score("Fidelity Growth Company Pool", "Exxon Mobil Corporation") < 0.40


# ── Pipeline stages ──────────────────────────────────────────────────────────

def test_working_symbol_is_left_alone(session, monkeypatch):
    _hold(session, 1, "ZZZZ", "Some New Fund")     # valid form, no price rows yet
    _stub(monkeypatch, prices_ok={"ZZZZ"})
    monkeypatch.setattr(SymbolService, "_yahoo_search",
                        staticmethod(lambda q: (_ for _ in ()).throw(AssertionError("searched"))))
    report = SymbolService(session).resolve_and_fix()
    assert report["verified_ok"] == [{"symbol": "ZZZZ", "name": "Some New Fund"}]
    assert session.get(Asset, 1).symbol == "ZZZZ"   # never remapped


def test_cusip_resolves_via_openfigi_with_provenance(session, monkeypatch):
    _hold(session, 1, "31617E745", "Fidelity 500 Index Fund")
    _stub(monkeypatch, figi={"31617E745": "FXAIX"}, prices_ok={"FXAIX"})
    report = SymbolService(session).resolve_and_fix()
    a = session.get(Asset, 1)
    assert a.symbol == "FXAIX" and a.cusip == "31617E745"
    assert a.resolution_source == "openfigi:31617E745"
    assert report["resolved"][0]["via"] == "openfigi"


def test_figi_ticker_is_normalised_to_yahoo_form(session, monkeypatch):
    _hold(session, 1, "084670702", "Berkshire Hathaway Inc Class B")
    _stub(monkeypatch, figi={"084670702": "BRK/B"}, prices_ok={"BRK-B"})
    SymbolService(session).resolve_and_fix()
    assert session.get(Asset, 1).symbol == "BRK-B"


def test_name_search_auto_applies_high_confidence(session, monkeypatch):
    _hold(session, 1, "14022L645", "iShares Core S&P 500 ETF")
    _stub(monkeypatch, search=[_quote("IVV", "iShares Core S&P 500 ETF")],
          prices_ok={"IVV"})
    report = SymbolService(session).resolve_and_fix()
    a = session.get(Asset, 1)
    assert a.symbol == "IVV" and a.cusip == "14022L645"
    assert a.resolution_source == "yahoo-search:name"
    assert "name match" in report["resolved"][0]["via"]


def test_name_search_stores_medium_confidence_as_suggestion(session, monkeypatch):
    _hold(session, 1, "BRKB", "Berkshire Hathaway Class B")
    _stub(monkeypatch, search=[_quote("BRK-B", "Berkshire Hathaway Inc. New", qt="EQUITY", exch="NYQ")],
          prices_ok={"BRK-B"})
    report = SymbolService(session).resolve_and_fix()
    a = session.get(Asset, 1)
    assert a.symbol == "BRKB"                       # unchanged until user confirms
    assert a.suggested_symbol == "BRK-B"
    assert a.resolution_status == "suggested"
    assert "name match" in a.suggested_note
    assert report["suggested"][0]["suggested"] == "BRK-B"


def test_foreign_and_wrong_type_candidates_are_ignored(session, monkeypatch):
    _hold(session, 1, "14022L645", "iShares Core S&P 500 ETF")
    _stub(monkeypatch, search=[
        _quote("GSPX.L", "ISHARES VII PLC ISH S&P500 UCIT", exch="LSE"),   # foreign venue
        _quote("SPYOPT", "iShares Core S&P 500 ETF", qt="OPTION"),         # wrong type
    ], prices_ok={"GSPX.L", "SPYOPT"})
    report = SymbolService(session).resolve_and_fix()
    assert report["resolved"] == [] and report["suggested"] == []
    assert session.get(Asset, 1).resolution_status == "no_listing"


def test_dead_end_cusip_marked_no_listing_and_skipped(session, monkeypatch):
    _hold(session, 1, "31617E745", "FIAM Blend Target Date 2040 T")
    _stub(monkeypatch)                              # nothing resolves anywhere
    report = SymbolService(session).resolve_and_fix()
    a = session.get(Asset, 1)
    assert a.resolution_status == "no_listing"
    assert report["no_listing"] == [{"symbol": "31617E745", "name": a.name}]

    # Later runs skip it entirely — no search, no FIGI…
    monkeypatch.setattr(SymbolService, "_yahoo_search",
                        staticmethod(lambda q: (_ for _ in ()).throw(AssertionError("searched"))))
    second = SymbolService(session).resolve_and_fix()
    assert all(not v for v in second.values())
    # …unless forced.
    _stub(monkeypatch, search=[_quote("FFBZX", "FIAM Blend Target Date 2040 T", qt="MUTUALFUND", exch="NMS")],
          prices_ok={"FFBZX"})
    forced = SymbolService(session).resolve_and_fix(force=True)
    assert forced["resolved"] and session.get(Asset, 1).symbol == "FFBZX"


def test_well_formed_dead_ticker_stays_eligible(session, monkeypatch):
    _hold(session, 1, "ZZZZ", "Mystery Position")
    _stub(monkeypatch)                              # doesn't price, nothing found
    report = SymbolService(session).resolve_and_fix()
    a = session.get(Asset, 1)
    assert report["unresolved"] == [{"symbol": "ZZZZ", "name": "Mystery Position"}]
    assert a.resolution_status is None              # not terminal — retried next run


def test_apply_suggestion_roundtrip(session, monkeypatch):
    a = _hold(session, 1, "BRKB", "Berkshire Hathaway Class B")
    a.suggested_symbol = "BRK-B"
    a.suggested_note = "Berkshire Hathaway Inc. New (Equity, NYQ) — 82% name match"
    a.resolution_status = "suggested"
    session.flush()

    result = SymbolService(session).apply_suggestion(1)
    assert result == {"asset_id": 1, "from": "BRKB", "ticker": "BRK-B",
                      "name": "Berkshire Hathaway Class B"}
    assert a.symbol == "BRK-B"
    assert a.resolution_source.startswith("user-confirmed")
    assert a.suggested_symbol is None and a.resolution_status is None

    assert SymbolService(session).apply_suggestion(1) is None    # nothing left to apply
    assert SymbolService(session).apply_suggestion(999) is None  # unknown asset


# ── Network guards on the real backends ──────────────────────────────────────

def test_prices_ok_gated_by_render_guard(session):
    svc = SymbolService(session)
    with patch("yfinance.Ticker", side_effect=AssertionError("network hit")):
        assert svc._prices_ok("SPY") is False       # outside allow_network: no fetch

    class FakeTicker:
        def __init__(self, sym): pass
        def history(self, period):
            idx = pd.date_range(end=date.today(), periods=5, freq="D")
            return pd.DataFrame({"Close": np.linspace(1, 2, 5)}, index=idx)

    svc2 = SymbolService(session)
    with allow_network(), patch("yfinance.Ticker", FakeTicker):
        assert svc2._prices_ok("SPY") is True
    with patch("yfinance.Ticker", side_effect=AssertionError("network hit")):
        assert svc2._prices_ok("SPY") is True       # cached per run


def test_search_and_figi_gated_by_render_guard():
    # Outside allow_network both backends return empty without touching the wire.
    with patch("yfinance.Search", side_effect=AssertionError("network hit")):
        assert SymbolService._yahoo_search("apple") == []
    with patch("urllib.request.urlopen", side_effect=AssertionError("network hit")):
        assert SymbolService._openfigi_map(["31617E745"]) == {}


# ── Dashboard banner (API) ───────────────────────────────────────────────────

def test_dashboard_banner_shows_suggestion_and_proxy_guidance(session, test_client):
    a = _hold(session, 11, "BRKB", "Berkshire Hathaway Class B")
    a.suggested_symbol = "BRK-B"
    a.suggested_note = "Berkshire Hathaway Inc. New (Equity, NYQ) — 82% name match"
    a.resolution_status = "suggested"
    b = _hold(session, 12, "31617E745", "FIAM Blend Target Date 2040 T")
    b.resolution_status = "no_listing"
    session.commit()

    html = test_client.get("/").text
    assert "Use BRK-B" in html                       # one-click apply button
    assert "82% name match" in html
    assert "no public listing found" in html         # proxy guidance for the CIT
    assert "set a price proxy" in html

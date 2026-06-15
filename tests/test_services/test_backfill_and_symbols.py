"""Tests for price-history backfill and CUSIP/unpriceable-symbol resolution."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.market_data import MarketDataService
from app.services.symbol_service import SymbolService, looks_like_cusip


def _hold(session, aid, symbol, cls=AssetClassEnum.US_EQUITY):
    a = Asset(id=aid, symbol=symbol, name=f"{symbol} fund", asset_class=cls, is_publicly_traded=True)
    acct = session.query(Account).first() or Account(id=1, name="B", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([a, acct]); session.flush()
    txn = Transaction(account_id=acct.id, asset_id=aid, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2022, 1, 1), quantity=Decimal("100"), price_per_unit=Decimal("100"),
                      total_amount=Decimal("10000"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=acct.id, asset_id=aid, acquisition_date=date(2022, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal("100"),
                       remaining_quantity=Decimal("100"), cost_basis_per_unit=Decimal("100"),
                       original_cost_basis_per_unit=Decimal("100")))
    session.flush()
    return a


# ── Backfill ───────────────────────────────────────────────────────────────

def test_backfill_persists_and_is_idempotent(session):
    _hold(session, 1, "VOO")
    md = MarketDataService(session)
    assert md.history_is_thin() is True

    idx = pd.date_range(end=date.today(), periods=500, freq="D")
    df = pd.DataFrame({"Close": np.linspace(80, 100, 500)}, index=idx)
    with patch("yfinance.download", return_value=df):
        first = md.backfill_history(months=24)
        second = md.backfill_history(months=24)

    assert first["rows_added"] == 500
    assert second["rows_added"] == 0                     # only missing dates inserted
    assert session.query(AssetPrice).filter_by(asset_id=1).count() == 500
    assert md.history_is_thin() is False


def test_history_thin_is_false_without_holdings(session):
    assert MarketDataService(session).history_is_thin() is False     # no-op on empty DB


def test_backfill_skips_stable_value(session):
    _hold(session, 1, "CASH", AssetClassEnum.CASH)
    # Stable-value is excluded → no symbols to download, no network touched.
    with patch("yfinance.download", side_effect=AssertionError("should not be called")):
        res = MarketDataService(session).backfill_history()
    assert res == {"symbols": 0, "rows_added": 0}


# ── CUSIP / symbol resolution ────────────────────────────────────────────────

def test_looks_like_cusip():
    assert looks_like_cusip("31617E745") is True
    assert looks_like_cusip("VOO") is False
    assert looks_like_cusip("BRKB") is False
    assert looks_like_cusip(None) is False
    assert looks_like_cusip("AAPLAAPLA") is False        # 9 letters, no digit → not a CUSIP


def test_unpriceable_detects_cusip_and_no_price(session):
    _hold(session, 1, "31617E745")                        # CUSIP symbol
    _hold(session, 2, "ZZZZ")                             # valid-looking but no price rows
    _hold(session, 3, "VOO")
    session.add(AssetPrice(asset_id=3, price_date=date.today(), close_price=Decimal("100"), source="t"))
    session.flush()
    syms = {a.symbol for a in SymbolService(session).unpriceable_assets()}
    assert syms == {"31617E745", "ZZZZ"}                  # VOO has a price → priceable


def test_resolve_and_fix_maps_cusip_to_ticker(session):
    a = _hold(session, 1, "31617E745")
    with patch.object(SymbolService, "_openfigi_map", staticmethod(lambda cusips: {"31617E745": "CRM"})):
        report = SymbolService(session).resolve_and_fix()
    assert report["resolved"] == [{"cusip": "31617E745", "ticker": "CRM", "name": a.name}]
    assert a.symbol == "CRM" and a.cusip == "31617E745"   # CUSIP preserved in its own field


def test_resolve_reports_unresolved(session):
    _hold(session, 1, "ZZZZ")                             # unknown ticker, no price, not a CUSIP
    with patch.object(SymbolService, "_openfigi_map", staticmethod(lambda cusips: {})):
        report = SymbolService(session).resolve_and_fix()
    assert report["resolved"] == []
    assert report["unresolved"] and report["unresolved"][0]["symbol"] == "ZZZZ"

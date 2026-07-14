"""Regressions for the crash + duplicate storm seen in the wild (v0.1.13 logs).

Three failures, one root: the Daily Brief render called SnapshotService.capture()
inline, which fetches a quote per held symbol and then WRITES.

  1. "Failed to get ticker 'BRK/B'" / 404s for CUSIPs and CRM-TOTAL — junk
     symbols sent to Yahoo on every capture.
  2. "UNIQUE constraint failed: price_snapshots..." — check-then-insert raced a
     concurrent capture.
  3. "database is locked" → PendingRollbackError → HTTP 500 — the write fought
     the backfill for SQLite's single write lock, and the swallowed error left
     the request's session un-committable.
"""

from datetime import date, datetime
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice, PriceSnapshot
from app.services.snapshot_service import SnapshotService
from app.services.symbol_service import SymbolService
from app.services.symbols import looks_like_rollup, yahoo_symbol


# ── 1. Symbols we must never send to a price feed ────────────────────────────

def test_yahoo_symbol_normalises_class_shares():
    assert yahoo_symbol("BRK/B") == "BRK-B"    # the exact log line: ticker 'BRK/B'
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("brk/b") == "BRK-B"
    assert yahoo_symbol("BRK-B") == "BRK-B"


def test_yahoo_symbol_passes_real_tickers():
    for sym in ("AAPL", "VOO", "BTC-USD", "ETH-USD", "QQQ"):
        assert yahoo_symbol(sym) == sym


def test_yahoo_symbol_rejects_unpriceable():
    assert yahoo_symbol("31617E745") is None   # CUSIP (Fidelity Contrafund pool)
    assert yahoo_symbol("14022L645") is None   # CUSIP (Capital Group target date)
    assert yahoo_symbol("CRM-TOTAL") is None   # broker subtotal row, not a security
    assert yahoo_symbol("") is None
    assert yahoo_symbol(None) is None


def test_looks_like_rollup():
    assert looks_like_rollup("CRM-TOTAL") is True
    assert looks_like_rollup("AAPL") is False


def test_capture_skips_unpriceable_symbols_entirely(session, monkeypatch):
    """No network call at all for CUSIP / subtotal junk — that spam was one
    failed round-trip per symbol, per capture."""
    acct = Account(id=1, name="B", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add(acct)
    for i, sym in enumerate(("CRM-TOTAL", "31617E745"), start=1):
        session.add(Asset(id=i, symbol=sym, name=sym, asset_class=AssetClassEnum.US_EQUITY,
                          is_publicly_traded=True))
    session.flush()

    import yfinance as yf

    def _boom(*a, **k):
        raise AssertionError("must not query an unpriceable symbol")

    monkeypatch.setattr(yf, "Ticker", _boom)
    SnapshotService(session).capture(force=True)   # no holdings/lots → no-op, but must not raise


def test_unpriceable_assets_flags_rollup_and_cusip(session):
    from app.models.tax_lot import TaxLot
    from app.models.transaction import Transaction, TransactionTypeEnum

    acct = Account(id=1, name="B", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add(acct)
    session.add(Asset(id=1, symbol="CRM-TOTAL", name="CRM subtotal",
                      asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True))
    session.flush()
    txn = Transaction(account_id=1, asset_id=1, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2024, 1, 1), quantity=Decimal("1"),
                      price_per_unit=Decimal("1"), total_amount=Decimal("1"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=1, asset_id=1, acquisition_date=date(2024, 1, 1),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal("1"),
                       remaining_quantity=Decimal("1"), cost_basis_per_unit=Decimal("1"),
                       original_cost_basis_per_unit=Decimal("1")))
    session.flush()

    flagged = {a.symbol for a in SymbolService(session).unpriceable_assets()}
    assert "CRM-TOTAL" in flagged   # surfaced to the user instead of retried forever


# ── 2. The duplicate race ────────────────────────────────────────────────────

def test_upsert_snapshot_survives_a_duplicate_insert(session, monkeypatch):
    """Losing the check-then-insert race must update the row, not blow up the
    session (that IntegrityError is what 500'd the page at commit).

    The race: our lookup runs, sees nothing, and a concurrent capture inserts the
    row before our INSERT lands. Simulated by forcing the lookup to miss while the
    row really is there — without this the insert path is never reached.
    """
    a = Asset(id=1, symbol="AAPL", name="Apple", asset_class=AssetClassEnum.US_EQUITY,
              is_publicly_traded=True)
    session.add(a); session.flush()

    svc = SnapshotService(session)
    today, when = date.today(), datetime.now()
    svc._upsert_snapshot(1, today, "morning", Decimal("100"), when)
    session.flush()

    monkeypatch.setattr(svc, "_find_snapshot", lambda *a, **k: None)   # the racing read
    svc._upsert_snapshot(1, today, "morning", Decimal("222"), when)
    monkeypatch.undo()
    session.commit()          # must NOT raise PendingRollbackError

    rows = session.query(PriceSnapshot).filter_by(asset_id=1, price_date=today, bucket="morning").all()
    assert len(rows) == 1                     # no duplicate row
    assert rows[0].price == Decimal("100")    # the winner's row survived intact


def test_upsert_daily_close_survives_a_duplicate_insert(session, monkeypatch):
    a = Asset(id=1, symbol="AAPL", name="Apple", asset_class=AssetClassEnum.US_EQUITY,
              is_publicly_traded=True)
    session.add(a); session.flush()

    svc = SnapshotService(session)
    today = date.today()
    svc._upsert_daily_close(1, today, Decimal("100"))
    session.flush()

    monkeypatch.setattr(svc, "_find_daily_close", lambda *a, **k: None)
    svc._upsert_daily_close(1, today, Decimal("222"))
    monkeypatch.undo()
    session.commit()

    rows = session.query(AssetPrice).filter_by(asset_id=1, price_date=today).all()
    assert len(rows) == 1

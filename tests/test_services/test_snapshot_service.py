"""Tests for intraday price snapshots, stable-value pinning, and the
change-since-previous-reading computation."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice, PriceSnapshot
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.snapshot_service import SnapshotService, _bucket_for


def _add_holding(session, asset, qty, price):
    acct = session.query(Account).first()
    if not acct:
        acct = Account(name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
        session.add(acct); session.flush()
    txn = Transaction(
        account_id=acct.id, asset_id=asset.id, transaction_type=TransactionTypeEnum.BUY,
        transaction_date=date(2023, 1, 1), quantity=Decimal(qty), price_per_unit=Decimal(price),
        total_amount=Decimal(qty) * Decimal(price), fees=Decimal("0"),
    )
    session.add(txn); session.flush()
    session.add(TaxLot(
        account_id=acct.id, asset_id=asset.id, acquisition_date=date(2023, 1, 1),
        acquisition_transaction_id=txn.id, original_quantity=Decimal(qty),
        remaining_quantity=Decimal(qty), cost_basis_per_unit=Decimal(price),
        original_cost_basis_per_unit=Decimal(price),
    ))


def test_bucket_boundaries():
    assert _bucket_for(datetime(2026, 6, 15, 7, 0)) == "premarket"
    assert _bucket_for(datetime(2026, 6, 15, 9, 30)) == "morning"
    assert _bucket_for(datetime(2026, 6, 15, 11, 59)) == "morning"
    assert _bucket_for(datetime(2026, 6, 15, 12, 0)) == "midday"
    assert _bucket_for(datetime(2026, 6, 15, 16, 5)) == "evening"


def test_change_since_previous(session):
    voo = Asset(symbol="VOO", name="Vanguard", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(voo); session.flush()
    _add_holding(session, voo, "100", "90")
    today = SnapshotService._now_et().date()
    session.add(AssetPrice(asset_id=voo.id, price_date=today, close_price=Decimal("102"), source="test"))
    # Previous-evening reading @100, this-morning reading @102.
    session.add(PriceSnapshot(asset_id=voo.id, price_date=today - timedelta(days=1),
                              bucket="evening", price=Decimal("100"),
                              captured_at=datetime(2026, 6, 14, 16, 5)))
    session.add(PriceSnapshot(asset_id=voo.id, price_date=today,
                              bucket="morning", price=Decimal("102"),
                              captured_at=datetime(2026, 6, 15, 9, 40)))
    session.flush()

    ch = SnapshotService(session).change_since_previous()
    assert ch["available"] and ch["has_prev"]
    assert ch["current_label"] == "Morning"
    assert ch["since_label"].startswith("Evening")
    assert ch["change"] == 200.0                  # 100 shares × ($102 − $100)
    assert ch["change_pct"] == 2.0                # 200 / 10,000
    assert ch["total_aum"] == 10200.0


def test_single_reading_has_no_previous(session):
    voo = Asset(symbol="VOO", name="Vanguard", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(voo); session.flush()
    _add_holding(session, voo, "100", "90")
    today = SnapshotService._now_et().date()
    # Stored price so get_summary() values from the DB (no live yfinance call).
    session.add(AssetPrice(asset_id=voo.id, price_date=today, close_price=Decimal("102"), source="test"))
    session.add(PriceSnapshot(asset_id=voo.id, price_date=today, bucket="morning",
                              price=Decimal("102"), captured_at=SnapshotService._now_et()))
    session.flush()
    ch = SnapshotService(session).change_since_previous()
    assert ch["available"] and not ch["has_prev"]


def test_capture_pins_stable_value_to_one(session):
    """A CASH holding is priced at $1.00 — never from yfinance — and existing
    corrupt rows are overwritten. (CASH-only → no network.)"""
    cash = Asset(symbol="CASH", name="Cash", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    session.add(cash); session.flush()
    _add_holding(session, cash, "9300.95", "1")
    today = SnapshotService._now_et().date()
    # Pre-existing corruption: CASH priced as the PGIM ETF (~$83).
    session.add(AssetPrice(asset_id=cash.id, price_date=today, close_price=Decimal("83.03"), source="yfinance"))
    session.flush()

    SnapshotService(session).capture(force=True)

    snap = session.query(PriceSnapshot).filter_by(asset_id=cash.id).first()
    assert snap is not None and snap.price == Decimal("1")
    daily = session.query(AssetPrice).filter_by(asset_id=cash.id, price_date=today).first()
    assert daily.close_price == Decimal("1")           # corrupt $83 repaired forward


def test_opportunistic_capture_throttled(session):
    """force=False is a no-op when a recent reading already exists."""
    cash = Asset(symbol="CASH", name="Cash", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    session.add(cash); session.flush()
    _add_holding(session, cash, "100", "1")
    now = SnapshotService._now_et()
    session.add(PriceSnapshot(asset_id=cash.id, price_date=now.date(), bucket="morning",
                              price=Decimal("1"), captured_at=now))
    session.flush()
    before = session.query(PriceSnapshot).count()
    SnapshotService(session).capture(force=False)       # latest reading is fresh → skip
    assert session.query(PriceSnapshot).count() == before

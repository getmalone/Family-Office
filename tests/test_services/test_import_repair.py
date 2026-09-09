"""Tests for the retroactive cleanup of pre-v0.1.21 stacked imports."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum
from app.models.tax_lot import TaxLot, TaxLotDisposal
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services import import_repair
from app.services.import_service import IMPORT_NOTE

BASE = datetime(2026, 8, 1, 9, 0, 0)


def _account(session, name, **kw):
    acct = Account(name=name, account_type=AccountTypeEnum.BROKERAGE,
                   is_taxable=True, is_active=True, **kw)
    session.add(acct)
    session.flush()
    return acct


def _asset(session, symbol):
    asset = Asset(symbol=symbol, name=symbol, asset_class=AssetClassEnum.US_EQUITY,
                  is_publicly_traded=True)
    session.add(asset)
    session.flush()
    return asset


def _upload(session, account, holdings, when, note=IMPORT_NOTE):
    """Write one old-style import batch: a lot per holding, all at `when`."""
    lots = []
    for asset, qty, acquired in holdings:
        txn = Transaction(
            account_id=account.id, asset_id=asset.id,
            transaction_type=TransactionTypeEnum.BUY, transaction_date=acquired,
            quantity=Decimal(qty), price_per_unit=Decimal("10"),
            total_amount=Decimal(qty) * 10, notes=note, created_at=when,
        )
        session.add(txn)
        session.flush()
        lot = TaxLot(
            account_id=account.id, asset_id=asset.id, acquisition_date=acquired,
            acquisition_transaction_id=txn.id, original_quantity=Decimal(qty),
            remaining_quantity=Decimal(qty), cost_basis_per_unit=Decimal("10"),
            original_cost_basis_per_unit=Decimal("10"), cost_basis_method="fifo",
            is_closed=False, created_at=when,
        )
        session.add(lot)
        lots.append(lot)
    session.flush()
    return lots


def test_scan_is_quiet_when_imported_once(session):
    acct = _account(session, "Brokerage")
    _upload(session, acct, [(_asset(session, "VOO"), "100", date(2023, 3, 15))], BASE)
    report = import_repair.scan(session)
    assert report["removable"] == 0
    assert report["accounts"] == []


def test_repair_keeps_the_newest_upload_when_quantities_changed(session):
    """The real case: each re-upload carried updated figures, so the stacked
    copies are not identical and totals drifted upward."""
    acct = _account(session, "BrokerageLink")
    voo = _asset(session, "VOO")
    _upload(session, acct, [(voo, "425", date(2023, 3, 15))], BASE)
    _upload(session, acct, [(voo, "450", date(2023, 3, 15))], BASE + timedelta(days=30))
    _upload(session, acct, [(voo, "470", date(2023, 3, 15))], BASE + timedelta(days=60))
    assert sum(l.remaining_quantity for l in session.query(TaxLot).all()) == Decimal("1345")

    preview = import_repair.scan(session)
    assert preview["removable"] == 2
    assert preview["accounts"][0]["uploads"] == 3
    assert session.query(TaxLot).count() == 3  # scan did not mutate

    report = import_repair.repair(session)
    assert report["removable"] == 2
    lots = session.query(TaxLot).all()
    assert len(lots) == 1
    assert lots[0].original_quantity == Decimal("470")  # the latest figure, not the sum
    assert session.query(Transaction).count() == 1


def test_repair_keeps_every_lot_within_one_upload(session):
    """Two tax lots of the same symbol in a single file are legitimate."""
    acct = _account(session, "Brokerage")
    voo = _asset(session, "VOO")
    _upload(session, acct, [(voo, "100", date(2022, 1, 5)),
                            (voo, "60", date(2023, 7, 1))], BASE)
    _upload(session, acct, [(voo, "100", date(2022, 1, 5)),
                            (voo, "60", date(2023, 7, 1))], BASE + timedelta(days=10))

    import_repair.repair(session)
    lots = session.query(TaxLot).all()
    assert len(lots) == 2
    assert sorted(l.acquisition_date for l in lots) == [date(2022, 1, 5), date(2023, 7, 1)]


def test_repair_spares_superseded_lots_with_sale_history(session):
    """A superseded lot that has been sold from is realized-gain history: it
    stays, is reported as kept, and only its clean sibling is removed."""
    acct = _account(session, "Brokerage")
    voo, aapl = _asset(session, "VOO"), _asset(session, "AAPL")
    old = _upload(session, acct, [(voo, "100", date(2023, 3, 15)),
                                  (aapl, "50", date(2023, 3, 15))], BASE)
    session.add(TaxLotDisposal(
        tax_lot_id=old[0].id, sale_transaction_id=old[0].acquisition_transaction_id,
        quantity_disposed=Decimal("10"), proceeds_per_unit=Decimal("15"),
        realized_gain_loss=Decimal("50"), is_short_term=True,
        disposal_date=date(2024, 5, 1),
    ))
    session.flush()
    _upload(session, acct, [(voo, "120", date(2023, 3, 15)),
                            (aapl, "50", date(2023, 3, 15))], BASE + timedelta(days=30))

    report = import_repair.repair(session)
    assert (report["removable"], report["kept"]) == (1, 1)
    assert report["accounts"][0]["with_history"] == 1
    assert session.query(TaxLot).filter(TaxLot.id == old[0].id).first() is not None
    assert session.query(TaxLot).filter(TaxLot.id == old[1].id).first() is None
    assert session.query(TaxLot).count() == 3  # the kept lot + the newest upload


def test_fully_blocked_account_is_not_flagged(session):
    """Nothing removable means nothing to prompt the user about."""
    acct = _account(session, "Brokerage")
    voo = _asset(session, "VOO")
    old = _upload(session, acct, [(voo, "100", date(2023, 3, 15))], BASE)[0]
    old.remaining_quantity = Decimal("90")  # sold from since the import
    _upload(session, acct, [(voo, "100", date(2023, 3, 15))], BASE + timedelta(days=30))
    session.flush()

    report = import_repair.scan(session)
    assert (report["removable"], report["accounts"]) == (0, [])
    import_repair.repair(session)
    assert session.query(TaxLot).count() == 2


def test_repair_ignores_hand_entered_positions(session):
    acct = _account(session, "Brokerage")
    voo = _asset(session, "VOO")
    _upload(session, acct, [(voo, "100", date(2023, 3, 15))], BASE)
    _upload(session, acct, [(voo, "100", date(2023, 3, 15))], BASE + timedelta(days=30))
    _upload(session, acct, [(voo, "5", date(2024, 2, 2))], BASE + timedelta(days=45),
            note="Entered by hand")

    import_repair.repair(session)
    remaining = session.query(Transaction).all()
    assert sorted(t.notes for t in remaining) == ["Entered by hand", IMPORT_NOTE]


def test_repair_leaves_other_accounts_alone(session):
    clean = _account(session, "Untouched")
    dirty = _account(session, "Stacked")
    voo = _asset(session, "VOO")
    _upload(session, clean, [(voo, "10", date(2023, 3, 15))], BASE)
    _upload(session, dirty, [(voo, "20", date(2023, 3, 15))], BASE)
    _upload(session, dirty, [(voo, "25", date(2023, 3, 15))], BASE + timedelta(days=30))

    report = import_repair.repair(session)
    assert [a["account"] for a in report["accounts"]] == ["Stacked"]
    assert session.query(TaxLot).filter(TaxLot.account_id == clean.id).count() == 1
    assert session.query(TaxLot).filter(TaxLot.account_id == dirty.id).count() == 1

"""Tests for reactivating accounts on import and purging inactive ones."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.account import Account
from app.models.family import Ownership
from app.models.report import ComplianceFlag
from app.models.tax_lot import TaxLot, TaxLotDisposal, WashSaleAdjustment
from app.models.transaction import Transaction
from app.services import account_admin
from app.services.import_service import import_positions_csv

CSV = (
    "account,account_type,symbol,name,asset_class,quantity,cost_basis_total,acquired,price\n"
    "My Brokerage,brokerage,VOO,Vanguard S&P 500,us_equity,100,45000,2023-03-15,520.50\n"
)


def test_import_reactivates_a_deactivated_account(session):
    """Positions uploaded into a deactivated account used to disappear from
    holdings and AUM, which reads as 'it imported the security but not the
    account'."""
    import_positions_csv(session, CSV)
    acct = session.query(Account).filter(Account.name == "My Brokerage").first()
    acct.is_active = False
    session.flush()

    result = import_positions_csv(session, CSV)
    assert result["reactivated"] == 1
    assert result["accounts"] == 0  # reused, not duplicated
    session.refresh(acct)
    assert acct.is_active is True
    assert session.query(Account).count() == 1


def test_import_leaves_active_accounts_alone(session):
    import_positions_csv(session, CSV)
    result = import_positions_csv(session, CSV)
    assert result["reactivated"] == 0


def test_purge_refuses_while_the_account_is_active(session):
    import_positions_csv(session, CSV)
    acct = session.query(Account).filter(Account.name == "My Brokerage").first()
    with pytest.raises(ValueError):
        account_admin.purge_account(session, acct)
    assert session.query(Account).count() == 1


def test_purge_removes_the_account_and_all_its_history(session):
    import_positions_csv(session, CSV)
    acct = session.query(Account).filter(Account.name == "My Brokerage").first()
    lot = session.query(TaxLot).first()

    # Give it the full spread of dependent rows a real account accumulates.
    disposal = TaxLotDisposal(
        tax_lot_id=lot.id, sale_transaction_id=lot.acquisition_transaction_id,
        quantity_disposed=Decimal("10"), proceeds_per_unit=Decimal("600"),
        realized_gain_loss=Decimal("800"), is_short_term=False,
        disposal_date=date(2024, 6, 1),
    )
    session.add(disposal)
    session.flush()
    session.add(WashSaleAdjustment(
        disallowed_disposal_id=disposal.id, replacement_lot_id=lot.id,
        disallowed_loss=Decimal("100"), adjustment_date=date(2024, 6, 20),
    ))
    session.add(ComplianceFlag(
        flag_type="concentration", severity="low", description="test flag",
        related_transaction_id=lot.acquisition_transaction_id,
    ))
    session.add(Ownership(account_id=acct.id, ownership_pct=Decimal("100"),
                          effective_date=date(2023, 1, 1)))
    session.flush()

    footprint = account_admin.account_footprint(session, acct.id)
    assert (footprint["positions"], footprint["transactions"]) == (1, 1)

    acct.is_active = False
    session.flush()
    counts = account_admin.purge_account(session, acct)

    assert counts["name"] == "My Brokerage"
    assert session.query(Account).count() == 0
    assert session.query(TaxLot).count() == 0
    assert session.query(Transaction).count() == 0
    assert session.query(TaxLotDisposal).count() == 0
    assert session.query(WashSaleAdjustment).count() == 0
    assert session.query(Ownership).count() == 0
    # The finding still happened, so the flag survives with its link cleared.
    flag = session.query(ComplianceFlag).first()
    assert flag is not None and flag.related_transaction_id is None


def test_purge_only_touches_the_named_account(session):
    import_positions_csv(session, CSV)
    import_positions_csv(session, CSV.replace("My Brokerage", "Keep This"))
    doomed = session.query(Account).filter(Account.name == "My Brokerage").first()
    doomed.is_active = False
    session.flush()

    account_admin.purge_account(session, doomed)
    assert [a.name for a in session.query(Account).all()] == ["Keep This"]
    assert session.query(TaxLot).count() == 1
    assert session.query(Transaction).count() == 1

"""Permanently delete an account and everything that hangs off it.

"Delete" in the Manage Accounts screen is a *soft* delete — the account is
flagged inactive so its history stops counting toward AUM, holdings, and tax
views while remaining recoverable. That is the right default, but it leaves no
way to actually get rid of an account created by mistake (a stray name from a
bad import, say), and the rows keep turning up in anything that walks the tables
directly.

This module does the irreversible version. It is deliberately narrow: only an
already-inactive account can be purged, so a permanent delete is always a
second, deliberate step after deactivating.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.family import Ownership
from app.models.report import ComplianceFlag
from app.models.tax_lot import TaxLot, TaxLotDisposal, WashSaleAdjustment
from app.models.transaction import Transaction


def account_footprint(session: Session, account_id: int) -> dict:
    """Count what a purge would destroy, for the confirmation prompt."""
    return {
        "positions": session.query(TaxLot)
        .filter(TaxLot.account_id == account_id).count(),
        "transactions": session.query(Transaction)
        .filter(Transaction.account_id == account_id).count(),
    }


def footprints(session: Session, accounts: list[Account]) -> dict[int, dict]:
    """Footprints keyed by account id, for rendering a list of accounts."""
    return {a.id: account_footprint(session, a.id) for a in accounts}


def purge_account(session: Session, account: Account) -> dict:
    """Delete an inactive account and all of its history. Irreversible.

    Rows are removed child-first so nothing is ever left pointing at a missing
    parent: wash-sale adjustments, then disposals, then lots, then the
    transactions themselves. Compliance flags outlive the transaction they were
    raised against — the finding still happened — so their link is cleared
    rather than the flag deleted.

    Raises ValueError if the account is still active.
    """
    if account.is_active:
        raise ValueError("deactivate the account before deleting it permanently")

    counts = account_footprint(session, account.id)

    lot_ids = [r[0] for r in session.query(TaxLot.id)
               .filter(TaxLot.account_id == account.id).all()]
    txn_ids = [r[0] for r in session.query(Transaction.id)
               .filter(Transaction.account_id == account.id).all()]

    disposal_ids: list[int] = []
    if lot_ids or txn_ids:
        q = session.query(TaxLotDisposal.id)
        clauses = []
        if lot_ids:
            clauses.append(TaxLotDisposal.tax_lot_id.in_(lot_ids))
        if txn_ids:
            clauses.append(TaxLotDisposal.sale_transaction_id.in_(txn_ids))
        from sqlalchemy import or_
        disposal_ids = [r[0] for r in q.filter(or_(*clauses)).all()]

    if disposal_ids or lot_ids:
        from sqlalchemy import or_
        clauses = []
        if disposal_ids:
            clauses.append(WashSaleAdjustment.disallowed_disposal_id.in_(disposal_ids))
        if lot_ids:
            clauses.append(WashSaleAdjustment.replacement_lot_id.in_(lot_ids))
        (session.query(WashSaleAdjustment).filter(or_(*clauses))
         .delete(synchronize_session=False))

    if disposal_ids:
        (session.query(TaxLotDisposal).filter(TaxLotDisposal.id.in_(disposal_ids))
         .delete(synchronize_session=False))
    if lot_ids:
        (session.query(TaxLot).filter(TaxLot.id.in_(lot_ids))
         .delete(synchronize_session=False))
    if txn_ids:
        (session.query(ComplianceFlag)
         .filter(ComplianceFlag.related_transaction_id.in_(txn_ids))
         .update({ComplianceFlag.related_transaction_id: None},
                 synchronize_session=False))
        (session.query(Transaction).filter(Transaction.id.in_(txn_ids))
         .delete(synchronize_session=False))

    (session.query(Ownership).filter(Ownership.account_id == account.id)
     .delete(synchronize_session=False))

    counts["name"] = account.name
    session.delete(account)
    session.flush()
    return counts

"""One definition of "currently held": open tax lots in ACTIVE accounts.

Removing an account is a soft delete (``Account.is_active = False``) so its
transactions, closed lots, and realized gains survive for taxes and audit. But
its positions must vanish from every *current-value* view — AUM, holdings,
day-change, snapshots, the backfill/pricing universe. That rule used to be
re-implemented (or forgotten) query-by-query, which is how the dashboard kept
counting a removed account's $400k while the Accounts page didn't.

Every service that asks "what do we hold right now?" answers through this
module. Deliberately NOT routed through here: lot mechanics scoped to an
explicit account (disposal selection, closing a position) and the wash-sale
replacement-lot window — a replacement bought in any account still triggers a
wash sale, active or not.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.tax_lot import TaxLot


def open_lots_query(session: Session):
    """Base query for open lots held in active accounts."""
    return (
        session.query(TaxLot)
        .join(Account, Account.id == TaxLot.account_id)
        .filter(TaxLot.is_closed == False, Account.is_active == True)
    )


def held_asset_ids(session: Session) -> set[int]:
    """Distinct asset ids currently held in active accounts."""
    return {
        aid
        for (aid,) in session.query(TaxLot.asset_id)
        .join(Account, Account.id == TaxLot.account_id)
        .filter(TaxLot.is_closed == False, Account.is_active == True)
        .distinct()
    }

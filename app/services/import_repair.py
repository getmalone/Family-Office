"""Retroactive cleanup for positions duplicated by pre-v0.1.21 imports.

Before v0.1.21 every upload appended a fresh opening lot for each row, so
re-uploading an updated export stacked another full copy of the account on top
of the previous one and inflated its value. v0.1.21 made an import *replace* the
account's previous snapshot, but that only heals an account when a file is
uploaded for it — and an export that has since gone stale should not have to be
re-uploaded just to trigger the cleanup.

This module repairs the damage in place. Imported lots are grouped back into the
upload that created them (one upload writes all its rows within seconds), and
for each account only the newest upload is kept; earlier ones are removed. That
is exactly what today's importer would have done at upload time, applied after
the fact — so an account whose holdings changed between uploads lands on the
latest figures rather than the sum of every upload it ever received.

Never removed: accounts with only one import, positions entered by hand, and any
lot with real history (sold from, or referenced by a disposal, wash-sale
adjustment, or compliance flag). Those are reported as "kept" instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.tax_lot import TaxLot
from app.services.import_service import (
    delete_imported_lot,
    imported_lots,
    lot_has_history,
)

# Rows written further apart than this came from separate uploads. One upload
# writes every row in well under a second, while a person re-uploading a
# corrected file takes minutes at least.
BATCH_GAP = timedelta(seconds=60)


def _ts(lot: TaxLot) -> datetime:
    """Comparable creation time: tz-naive, and ordered even if it is missing."""
    created = getattr(lot, "created_at", None)
    if created is None:
        return datetime.min
    return created.replace(tzinfo=None) if created.tzinfo else created


def _batches(lots: list[TaxLot]) -> list[list[TaxLot]]:
    """Split one account's imported lots into the uploads that created them."""
    batches: list[list[TaxLot]] = []
    for lot in sorted(lots, key=lambda l: (_ts(l), l.id)):
        if batches and _ts(lot) - _ts(batches[-1][-1]) <= BATCH_GAP:
            batches[-1].append(lot)
        else:
            batches.append([lot])
    return batches


def scan(session: Session) -> dict:
    """Report what a repair would remove. Read-only — never mutates.

    Returns ``{"accounts": [...], "removable": int, "kept": int}`` where each
    account entry carries its name, how many uploads it accumulated, and how
    many positions would go away.
    """
    by_account: dict[int, list[TaxLot]] = {}
    for lot in imported_lots(session):
        by_account.setdefault(lot.account_id, []).append(lot)

    accounts: list[dict] = []
    removable = kept = 0
    for account_id, lots in by_account.items():
        batches = _batches(lots)
        if len(batches) < 2:
            continue  # imported once — nothing stacked
        stale = [lot for batch in batches[:-1] for lot in batch]
        to_remove = [l for l in stale if not lot_has_history(session, l)]
        to_keep = len(stale) - len(to_remove)
        if not to_remove:
            continue
        account = session.get(Account, account_id)
        accounts.append({
            "account_id": account_id,
            "account": account.name if account else f"account {account_id}",
            "uploads": len(batches),
            "remove": len(to_remove),
            "keep": len(batches[-1]) + to_keep,
            "with_history": to_keep,
        })
        removable += len(to_remove)
        kept += to_keep

    accounts.sort(key=lambda a: -a["remove"])
    return {"accounts": accounts, "removable": removable, "kept": kept}


def repair(session: Session) -> dict:
    """Remove superseded imports, keeping each account's most recent upload.

    Returns the same shape as :func:`scan`, describing what was actually done.
    """
    report = scan(session)
    if not report["removable"]:
        return report

    for entry in report["accounts"]:
        lots = imported_lots(session, entry["account_id"])
        batches = _batches(lots)
        for batch in batches[:-1]:
            for lot in batch:
                if not lot_has_history(session, lot):
                    delete_imported_lot(session, lot)
    session.flush()
    return report

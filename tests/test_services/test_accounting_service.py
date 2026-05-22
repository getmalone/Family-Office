"""Tests for the Accounting Service."""

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.accounting import JournalEntryCreate, JournalLineCreate
from app.services.accounting_service import AccountingService


def test_create_balanced_journal_entry(seeded_session):
    """Balanced journal entry is created successfully."""
    svc = AccountingService(seeded_session)

    # Get COA entries
    from app.models.journal import ChartOfAccounts
    cash = seeded_session.query(ChartOfAccounts).filter(ChartOfAccounts.code == "1010").one()
    inv = seeded_session.query(ChartOfAccounts).filter(ChartOfAccounts.code == "1200").one()

    entry = svc.create_journal_entry(JournalEntryCreate(
        entry_date=date.today(),
        description="Test buy",
        lines=[
            JournalLineCreate(coa_id=inv.id, debit=Decimal("10000"), credit=Decimal("0")),
            JournalLineCreate(coa_id=cash.id, debit=Decimal("0"), credit=Decimal("10000")),
        ],
    ))

    assert entry.id is not None
    assert entry.is_posted is True
    assert entry.is_balanced


def test_unbalanced_journal_entry_raises(seeded_session):
    """Unbalanced journal entry raises ValueError."""
    svc = AccountingService(seeded_session)

    from app.models.journal import ChartOfAccounts
    cash = seeded_session.query(ChartOfAccounts).filter(ChartOfAccounts.code == "1010").one()

    with pytest.raises(ValueError, match="unbalanced"):
        svc.create_journal_entry(JournalEntryCreate(
            entry_date=date.today(),
            description="Bad entry",
            lines=[
                JournalLineCreate(coa_id=cash.id, debit=Decimal("100"), credit=Decimal("0")),
            ],
        ))


def test_trial_balance(seeded_session):
    """Trial balance reports correct totals."""
    svc = AccountingService(seeded_session)
    tb = svc.get_trial_balance()

    # Should be balanced (even if empty)
    assert tb.is_balanced
    assert tb.total_debits == tb.total_credits

"""
Accounting Agent tools — LangChain tool wrappers around AccountingService.

These tools enable the Accounting & Ledger Agent to maintain double-entry
bookkeeping, categorize transactions, and produce financial statements.
"""

from datetime import date
from decimal import Decimal

from langchain_core.tools import tool


def create_accounting_tools(session_factory):
    """Create accounting tools with injected session factory."""

    @tool
    def get_trial_balance(as_of_date: str | None = None) -> str:
        """Get the trial balance as of a given date (YYYY-MM-DD format)."""
        from app.services.accounting_service import AccountingService

        with session_factory() as session:
            svc = AccountingService(session)
            dt = date.fromisoformat(as_of_date) if as_of_date else None
            tb = svc.get_trial_balance(dt)
            return tb.model_dump_json()

    @tool
    def get_income_statement(period_start: str, period_end: str) -> str:
        """Get income statement for a date range (YYYY-MM-DD format)."""
        from app.services.accounting_service import AccountingService

        with session_factory() as session:
            svc = AccountingService(session)
            stmt = svc.get_income_statement(
                date.fromisoformat(period_start),
                date.fromisoformat(period_end),
            )
            return stmt.model_dump_json()

    @tool
    def get_balance_sheet(as_of_date: str | None = None) -> str:
        """Get the balance sheet as of a given date."""
        from app.services.accounting_service import AccountingService

        with session_factory() as session:
            svc = AccountingService(session)
            dt = date.fromisoformat(as_of_date) if as_of_date else None
            bs = svc.get_balance_sheet(dt)
            return bs.model_dump_json()

    @tool
    def get_journal_entries(limit: int = 20) -> str:
        """Get recent journal entries."""
        from app.services.accounting_service import AccountingService

        with session_factory() as session:
            svc = AccountingService(session)
            entries = svc.get_journal_entries(limit=limit)
            import json
            return json.dumps([e.model_dump(mode="json") for e in entries])

    return [get_trial_balance, get_income_statement, get_balance_sheet, get_journal_entries]

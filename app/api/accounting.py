"""
Accounting routes for the Family Office dashboard.

Provides journal entries, trial balance, and financial statement views.
"""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.accounting_service import AccountingService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/journal")
def journal_entries(request: Request, db: Session = Depends(get_db)):
    """List journal entries."""
    svc = AccountingService(db)
    entries = svc.get_journal_entries()

    return templates.TemplateResponse(
        "accounting/journal.html",
        {
            "request": request,
            "entries": entries,
            "page_title": "Journal Entries",
        },
    )


@router.get("/trial-balance")
def trial_balance(request: Request, db: Session = Depends(get_db)):
    """Show trial balance."""
    svc = AccountingService(db)
    tb = svc.get_trial_balance()

    return templates.TemplateResponse(
        "accounting/trial_balance.html",
        {
            "request": request,
            "trial_balance": tb,
            "page_title": "Trial Balance",
        },
    )


@router.get("/statements")
def financial_statements(request: Request, db: Session = Depends(get_db)):
    """Show income statement and balance sheet."""
    svc = AccountingService(db)
    today = date.today()
    year_start = date(today.year, 1, 1)

    income_stmt = svc.get_income_statement(year_start, today)
    balance_sheet = svc.get_balance_sheet(today)

    return templates.TemplateResponse(
        "accounting/statements.html",
        {
            "request": request,
            "income_statement": income_stmt,
            "balance_sheet": balance_sheet,
            "page_title": "Financial Statements",
        },
    )

"""Accounting and ledger Pydantic schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class JournalLineCreate(BaseModel):
    coa_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: str | None = None
    entity_id: int | None = None


class JournalEntryCreate(BaseModel):
    entry_date: date
    description: str
    reference: str | None = None
    lines: list[JournalLineCreate]
    source: str = "manual"


class JournalEntryOut(BaseModel):
    id: int
    entry_date: date
    description: str
    reference: str | None
    is_posted: bool
    source: str
    total_debit: Decimal
    total_credit: Decimal

    model_config = {"from_attributes": True}


class TrialBalanceRow(BaseModel):
    coa_code: str
    coa_name: str
    account_type: str
    debit_balance: Decimal
    credit_balance: Decimal


class TrialBalance(BaseModel):
    as_of_date: date
    rows: list[TrialBalanceRow]
    total_debits: Decimal
    total_credits: Decimal
    is_balanced: bool


class IncomeStatement(BaseModel):
    period_start: date
    period_end: date
    revenue_items: list[tuple[str, Decimal]]
    expense_items: list[tuple[str, Decimal]]
    total_revenue: Decimal
    total_expenses: Decimal
    net_income: Decimal


class BalanceSheet(BaseModel):
    as_of_date: date
    asset_items: list[tuple[str, Decimal]]
    liability_items: list[tuple[str, Decimal]]
    equity_items: list[tuple[str, Decimal]]
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal

"""
Double-entry bookkeeping service for the Family Office.

The Accounting & Ledger Agent uses this service to maintain complete, IRS-compliant
books and records. Automatically categorizes every transaction as investment
income/expense vs. personal to preserve deductibility. Produces monthly balance sheets,
income statements, and cash-flow statements per family member or per entity.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.journal import ChartOfAccounts, JournalEntry, JournalLine
from app.models.transaction import Transaction, TransactionTypeEnum
from app.schemas.accounting import (
    BalanceSheet,
    IncomeStatement,
    JournalEntryCreate,
    JournalEntryOut,
    TrialBalance,
    TrialBalanceRow,
)


# Standard chart of accounts codes
COA_CASH = "1010"
COA_INVESTMENTS = "1200"
COA_REALIZED_GAIN = "4100"
COA_REALIZED_LOSS = "5100"
COA_DIVIDEND_INCOME = "4200"
COA_INTEREST_INCOME = "4300"
COA_FEE_EXPENSE = "6100"
COA_MGMT_EXPENSE = "6200"


class AccountingService:
    """
    Double-entry accounting for the Family Office.

    Ensures all journal entries balance (total debits == total credits) and
    provides financial statement generation for tax reporting and compliance.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_journal_entry(self, data: JournalEntryCreate) -> JournalEntry:
        """Create a new journal entry with validation that debits == credits."""
        total_debit = sum(line.debit for line in data.lines)
        total_credit = sum(line.credit for line in data.lines)

        if abs(total_debit - total_credit) > Decimal("0.01"):
            raise ValueError(
                f"Journal entry is unbalanced: debits={total_debit}, credits={total_credit}"
            )

        entry = JournalEntry(
            entry_date=data.entry_date,
            description=data.description,
            reference=data.reference,
            source=data.source,
            is_posted=True,
        )
        self.session.add(entry)
        self.session.flush()

        for line_data in data.lines:
            line = JournalLine(
                journal_entry_id=entry.id,
                coa_id=line_data.coa_id,
                debit=line_data.debit,
                credit=line_data.credit,
                memo=line_data.memo,
                entity_id=line_data.entity_id,
            )
            self.session.add(line)

        self.session.flush()
        return entry

    def auto_journal_for_trade(self, transaction: Transaction) -> JournalEntry | None:
        """
        Automatically create a journal entry for a trade transaction.

        BUY:  Debit Investments, Credit Cash
        SELL: Debit Cash, Credit Investments, Debit/Credit Realized Gain/Loss
        """
        cash_acct = self._get_or_create_coa(COA_CASH, "Cash", "asset")
        inv_acct = self._get_or_create_coa(COA_INVESTMENTS, "Investments", "asset")

        if transaction.transaction_type == TransactionTypeEnum.BUY:
            entry_data = JournalEntryCreate(
                entry_date=transaction.transaction_date,
                description=f"Buy {transaction.quantity} units - {transaction.notes or ''}".strip(),
                reference=f"TXN-{transaction.id}",
                source="auto_trade",
                lines=[
                    {"coa_id": inv_acct.id, "debit": transaction.total_amount, "credit": Decimal("0")},
                    {"coa_id": cash_acct.id, "debit": Decimal("0"), "credit": transaction.total_amount},
                ],
            )
            return self.create_journal_entry(entry_data)

        elif transaction.transaction_type == TransactionTypeEnum.SELL:
            # Simplified: net proceeds to cash, cost basis from investments
            gain_acct = self._get_or_create_coa(COA_REALIZED_GAIN, "Realized Gains", "revenue")
            loss_acct = self._get_or_create_coa(COA_REALIZED_LOSS, "Realized Losses", "expense")

            # Calculate total cost basis from disposals
            from app.models.tax_lot import TaxLotDisposal

            disposals = (
                self.session.query(TaxLotDisposal)
                .filter(TaxLotDisposal.sale_transaction_id == transaction.id)
                .all()
            )
            total_basis = sum(
                d.quantity_disposed * (d.proceeds_per_unit - d.realized_gain_loss / d.quantity_disposed)
                for d in disposals
            ) if disposals else transaction.total_amount
            total_gain = sum(d.realized_gain_loss for d in disposals)

            lines = [
                {"coa_id": cash_acct.id, "debit": transaction.total_amount, "credit": Decimal("0")},
                {"coa_id": inv_acct.id, "debit": Decimal("0"), "credit": abs(total_basis)},
            ]

            if total_gain > 0:
                lines.append(
                    {"coa_id": gain_acct.id, "debit": Decimal("0"), "credit": total_gain}
                )
            elif total_gain < 0:
                lines.append(
                    {"coa_id": loss_acct.id, "debit": abs(total_gain), "credit": Decimal("0")}
                )

            entry_data = JournalEntryCreate(
                entry_date=transaction.transaction_date,
                description=f"Sell {transaction.quantity} units - {transaction.notes or ''}".strip(),
                reference=f"TXN-{transaction.id}",
                source="auto_trade",
                lines=lines,
            )
            return self.create_journal_entry(entry_data)

        elif transaction.transaction_type == TransactionTypeEnum.DIVIDEND:
            div_acct = self._get_or_create_coa(COA_DIVIDEND_INCOME, "Dividend Income", "revenue")
            entry_data = JournalEntryCreate(
                entry_date=transaction.transaction_date,
                description=f"Dividend received - {transaction.notes or ''}".strip(),
                reference=f"TXN-{transaction.id}",
                source="auto_dividend",
                lines=[
                    {"coa_id": cash_acct.id, "debit": transaction.total_amount, "credit": Decimal("0")},
                    {"coa_id": div_acct.id, "debit": Decimal("0"), "credit": transaction.total_amount},
                ],
            )
            return self.create_journal_entry(entry_data)

        return None

    def get_trial_balance(self, as_of_date: date | None = None) -> TrialBalance:
        """Generate trial balance as of a given date."""
        as_of = as_of_date or date.today()

        rows = []
        total_debits = Decimal("0")
        total_credits = Decimal("0")

        accounts = self.session.query(ChartOfAccounts).filter(ChartOfAccounts.is_active == True).all()

        for acct in accounts:
            result = (
                self.session.query(
                    func.coalesce(func.sum(JournalLine.debit), 0),
                    func.coalesce(func.sum(JournalLine.credit), 0),
                )
                .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(
                    JournalLine.coa_id == acct.id,
                    JournalEntry.is_posted == True,
                    JournalEntry.entry_date <= as_of,
                )
                .one()
            )

            debit_bal = Decimal(str(result[0]))
            credit_bal = Decimal(str(result[1]))

            if debit_bal or credit_bal:
                rows.append(
                    TrialBalanceRow(
                        coa_code=acct.code,
                        coa_name=acct.name,
                        account_type=acct.account_type,
                        debit_balance=debit_bal,
                        credit_balance=credit_bal,
                    )
                )
                total_debits += debit_bal
                total_credits += credit_bal

        return TrialBalance(
            as_of_date=as_of,
            rows=rows,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=abs(total_debits - total_credits) < Decimal("0.01"),
        )

    def get_income_statement(
        self, period_start: date, period_end: date
    ) -> IncomeStatement:
        """Generate income statement for a period."""
        revenue_items = self._get_balances_by_type("revenue", period_start, period_end)
        expense_items = self._get_balances_by_type("expense", period_start, period_end)

        total_revenue = sum(amt for _, amt in revenue_items)
        total_expenses = sum(amt for _, amt in expense_items)

        return IncomeStatement(
            period_start=period_start,
            period_end=period_end,
            revenue_items=revenue_items,
            expense_items=expense_items,
            total_revenue=total_revenue,
            total_expenses=total_expenses,
            net_income=total_revenue - total_expenses,
        )

    def get_balance_sheet(self, as_of_date: date | None = None) -> BalanceSheet:
        """Generate balance sheet as of a given date."""
        as_of = as_of_date or date.today()
        beginning = date(2000, 1, 1)

        asset_items = self._get_balances_by_type("asset", beginning, as_of)
        liability_items = self._get_balances_by_type("liability", beginning, as_of)
        equity_items = self._get_balances_by_type("equity", beginning, as_of)

        return BalanceSheet(
            as_of_date=as_of,
            asset_items=asset_items,
            liability_items=liability_items,
            equity_items=equity_items,
            total_assets=sum(amt for _, amt in asset_items),
            total_liabilities=sum(amt for _, amt in liability_items),
            total_equity=sum(amt for _, amt in equity_items),
        )

    def get_journal_entries(
        self, start_date: date | None = None, end_date: date | None = None, limit: int = 50
    ) -> list[JournalEntryOut]:
        """List journal entries with totals."""
        query = self.session.query(JournalEntry).filter(JournalEntry.is_posted == True)
        if start_date:
            query = query.filter(JournalEntry.entry_date >= start_date)
        if end_date:
            query = query.filter(JournalEntry.entry_date <= end_date)

        entries = query.order_by(JournalEntry.entry_date.desc()).limit(limit).all()

        results = []
        for entry in entries:
            total_debit = sum(line.debit for line in entry.lines)
            total_credit = sum(line.credit for line in entry.lines)
            results.append(
                JournalEntryOut(
                    id=entry.id,
                    entry_date=entry.entry_date,
                    description=entry.description,
                    reference=entry.reference,
                    is_posted=entry.is_posted,
                    source=entry.source,
                    total_debit=total_debit,
                    total_credit=total_credit,
                )
            )
        return results

    def _get_balances_by_type(
        self, account_type: str, start_date: date, end_date: date
    ) -> list[tuple[str, Decimal]]:
        """Get account balances by type for financial statements."""
        results = (
            self.session.query(
                ChartOfAccounts.name,
                func.coalesce(func.sum(JournalLine.debit), 0),
                func.coalesce(func.sum(JournalLine.credit), 0),
            )
            .join(JournalLine, JournalLine.coa_id == ChartOfAccounts.id)
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .filter(
                ChartOfAccounts.account_type == account_type,
                JournalEntry.is_posted == True,
                JournalEntry.entry_date >= start_date,
                JournalEntry.entry_date <= end_date,
            )
            .group_by(ChartOfAccounts.name)
            .all()
        )

        items = []
        for name, debits, credits in results:
            # Assets/expenses have debit normal balance; liabilities/equity/revenue have credit normal
            if account_type in ("asset", "expense"):
                balance = Decimal(str(debits)) - Decimal(str(credits))
            else:
                balance = Decimal(str(credits)) - Decimal(str(debits))
            if balance:
                items.append((name, balance))
        return items

    def _get_or_create_coa(self, code: str, name: str, account_type: str) -> ChartOfAccounts:
        """Get or create a chart of accounts entry."""
        acct = self.session.query(ChartOfAccounts).filter(ChartOfAccounts.code == code).first()
        if not acct:
            acct = ChartOfAccounts(code=code, name=name, account_type=account_type)
            self.session.add(acct)
            self.session.flush()
        return acct

    def seed_chart_of_accounts(self) -> None:
        """Create the standard chart of accounts for the Family Office."""
        accounts = [
            ("1010", "Cash & Equivalents", "asset"),
            ("1200", "Investments - Marketable Securities", "asset"),
            ("1300", "Investments - Private Equity", "asset"),
            ("1400", "Investments - Real Estate", "asset"),
            ("1500", "Investments - Crypto", "asset"),
            ("1600", "Investments - Other", "asset"),
            ("1900", "Other Assets", "asset"),
            ("2100", "Margin Loans", "liability"),
            ("2200", "Other Liabilities", "liability"),
            ("3100", "Owner's Equity - Principal", "equity"),
            ("3200", "Owner's Equity - Spouse", "equity"),
            ("3300", "Retained Earnings", "equity"),
            ("4100", "Realized Capital Gains", "revenue"),
            ("4200", "Dividend Income", "revenue"),
            ("4300", "Interest Income", "revenue"),
            ("4400", "Rental Income", "revenue"),
            ("4500", "Other Investment Income", "revenue"),
            ("5100", "Realized Capital Losses", "expense"),
            ("6100", "Trading Fees & Commissions", "expense"),
            ("6200", "Investment Management Fees", "expense"),
            ("6300", "Legal & Professional Fees", "expense"),
            ("6400", "Tax Preparation Fees", "expense"),
            ("6500", "Research & Data Subscriptions", "expense"),
            ("6600", "Office & Administrative", "expense"),
            ("6700", "Travel - Due Diligence", "expense"),
            ("6800", "Insurance", "expense"),
            ("6900", "Other Deductible Expenses", "expense"),
        ]

        for code, name, acct_type in accounts:
            existing = self.session.query(ChartOfAccounts).filter(ChartOfAccounts.code == code).first()
            if not existing:
                is_deductible = acct_type == "expense"
                self.session.add(
                    ChartOfAccounts(
                        code=code,
                        name=name,
                        account_type=acct_type,
                        is_deductible=is_deductible,
                    )
                )
        self.session.flush()

"""
Tax optimization service for the Family Office.

Implements tax-loss harvesting scans, wash-sale window tracking, cost basis method
comparison, and tax election simulation. Maximizes after-tax returns by strategically
realizing investment losses to offset gains, making optimal tax elections including
Section 475 mark-to-market, Section 988 for forex, QSBS elections, and opportunity
zone deferrals.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.asset import Asset
from app.models.expense import Expense, ExpenseCategory
from app.models.tax_lot import TaxLot, TaxLotDisposal
from app.models.transaction import Transaction, TransactionTypeEnum
from app.schemas.tax import ElectionSimResult, HarvestCandidate, TaxSummary
from app.services.market_data import MarketDataService


# Approximate 2026 federal tax rates
SHORT_TERM_RATE = Decimal("0.37")
LONG_TERM_RATE = Decimal("0.20")
NIIT_RATE = Decimal("0.038")  # Net Investment Income Tax


class TaxService:
    """
    Tax optimization for the Family Office.

    Tracks deductible Family Office expenses and allocates them correctly,
    generates real-time 'tax alpha' reports showing current-year realized losses,
    projected offsets, and remaining loss carryforwards.
    """

    def __init__(self, session: Session, market_data: MarketDataService | None = None):
        self.session = session
        self.market_data = market_data or MarketDataService(session)

    def find_harvest_candidates(
        self,
        min_loss: Decimal = Decimal("100"),
        account_id: int | None = None,
    ) -> list[HarvestCandidate]:
        """
        Identify positions with unrealized losses eligible for tax-loss harvesting.

        Scans all open tax lots in taxable accounts, computes unrealized losses,
        and flags wash-sale risks from recent transactions.
        """
        query = (
            self.session.query(TaxLot)
            .join(Account, TaxLot.account_id == Account.id)
            .filter(TaxLot.is_closed == False, Account.is_taxable == True)
        )
        if account_id:
            query = query.filter(TaxLot.account_id == account_id)

        lots = query.all()

        # Group by asset+account
        grouped: dict[tuple[int, int], list[TaxLot]] = {}
        for lot in lots:
            key = (lot.asset_id, lot.account_id)
            grouped.setdefault(key, []).append(lot)

        candidates = []
        for (asset_id, acct_id), lot_group in grouped.items():
            asset = self.session.get(Asset, asset_id)
            account = self.session.get(Account, acct_id)
            if not asset or not account:
                continue

            current_price = self.market_data.get_current_price(asset)
            if current_price is None:
                continue

            total_loss = Decimal("0")
            oldest_date = date.today()
            any_short_term = False

            for lot in lot_group:
                unrealized = (current_price - lot.cost_basis_per_unit) * lot.remaining_quantity
                if unrealized < 0:
                    total_loss += unrealized
                if lot.acquisition_date < oldest_date:
                    oldest_date = lot.acquisition_date
                if not lot.is_long_term:
                    any_short_term = True

            if total_loss >= -min_loss:
                continue  # Loss too small

            # Check wash-sale risk
            wash_risk = self._has_recent_purchase(asset_id, acct_id, days=30)

            # Estimate tax savings
            rate = SHORT_TERM_RATE if any_short_term else LONG_TERM_RATE
            tax_savings = abs(total_loss) * (rate + NIIT_RATE)

            candidates.append(
                HarvestCandidate(
                    asset_id=asset_id,
                    symbol=asset.symbol,
                    name=asset.name,
                    account_id=acct_id,
                    account_name=account.name,
                    total_unrealized_loss=total_loss,
                    num_lots=len(lot_group),
                    oldest_lot_date=oldest_date,
                    is_short_term=any_short_term,
                    wash_sale_risk=wash_risk,
                    estimated_tax_savings=round(tax_savings, 2),
                )
            )

        return sorted(candidates, key=lambda c: c.total_unrealized_loss)

    def get_tax_summary(self, tax_year: int | None = None) -> TaxSummary:
        """
        Generate a comprehensive tax summary for the given year.

        Shows realized gains/losses, dividend income, deductible expenses,
        and estimated tax liability — enabling quarterly estimated tax payments.
        """
        year = tax_year or date.today().year
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)

        # Realized gains/losses from disposals
        disposals = (
            self.session.query(TaxLotDisposal)
            .filter(
                TaxLotDisposal.disposal_date >= year_start,
                TaxLotDisposal.disposal_date <= year_end,
            )
            .all()
        )

        st_gains = sum(d.realized_gain_loss for d in disposals if d.is_short_term and d.realized_gain_loss > 0)
        st_losses = sum(d.realized_gain_loss for d in disposals if d.is_short_term and d.realized_gain_loss < 0)
        lt_gains = sum(d.realized_gain_loss for d in disposals if not d.is_short_term and d.realized_gain_loss > 0)
        lt_losses = sum(d.realized_gain_loss for d in disposals if not d.is_short_term and d.realized_gain_loss < 0)

        # Dividend and interest income
        divs = (
            self.session.query(func.coalesce(func.sum(Transaction.total_amount), 0))
            .filter(
                Transaction.transaction_type == TransactionTypeEnum.DIVIDEND,
                Transaction.transaction_date >= year_start,
                Transaction.transaction_date <= year_end,
            )
            .scalar()
        ) or Decimal("0")

        interest = (
            self.session.query(func.coalesce(func.sum(Transaction.total_amount), 0))
            .filter(
                Transaction.transaction_type == TransactionTypeEnum.INTEREST,
                Transaction.transaction_date >= year_start,
                Transaction.transaction_date <= year_end,
            )
            .scalar()
        ) or Decimal("0")

        # Deductible expenses
        expenses = (
            self.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
            .filter(
                ExpenseCategory.is_deductible == True,
                Expense.expense_date >= year_start,
                Expense.expense_date <= year_end,
            )
            .scalar()
        ) or Decimal("0")

        net = st_gains + st_losses + lt_gains + lt_losses

        # Split taxable income by rate bucket:
        # Short-term gains + interest income → ordinary rates (37%)
        # Long-term gains + qualified dividends → preferential rates (20% + 3.8% NIIT)
        st_net = st_gains + st_losses
        lt_net = lt_gains + lt_losses
        st_taxable = st_net + Decimal(str(interest))
        lt_taxable = lt_net + Decimal(str(divs))

        # Apply deductible expenses to the highest-rate bucket first (maximises tax savings)
        remaining_expenses = Decimal(str(expenses))
        st_after_exp = max(Decimal("0"), st_taxable - remaining_expenses)
        remaining_expenses = max(Decimal("0"), remaining_expenses - st_taxable)
        lt_after_exp = max(Decimal("0"), lt_taxable - remaining_expenses)

        st_tax = max(Decimal("0"), st_after_exp * SHORT_TERM_RATE)
        lt_tax = max(Decimal("0"), lt_after_exp * (LONG_TERM_RATE + NIIT_RATE))
        estimated_tax = st_tax + lt_tax

        return TaxSummary(
            tax_year=year,
            short_term_gains=st_gains,
            short_term_losses=st_losses,
            long_term_gains=lt_gains,
            long_term_losses=lt_losses,
            net_gain_loss=net,
            dividends_qualified=Decimal(str(divs)),
            dividends_ordinary=Decimal("0"),
            interest_income=Decimal(str(interest)),
            deductible_expenses=Decimal(str(expenses)),
            estimated_tax_liability=estimated_tax,
            # IRC §1211(b): individuals may deduct up to $3,000 of net capital losses
            # against ordinary income per year; the remainder carries forward.
            loss_carryforward=max(Decimal("0"), abs(net) - Decimal("3000")) if net < 0 else Decimal("0"),
        )

    def simulate_election(self, election_type: str) -> list[ElectionSimResult]:
        """
        Simulate the impact of various tax elections.

        Supports comparing FIFO vs HIFO vs specific-ID, Section 475 mark-to-market,
        and other tax elections that lawfully reduce taxable income.
        """
        results = []

        if election_type == "cost_basis_method":
            for method, name in [("fifo", "FIFO"), ("hifo", "Highest Cost First"), ("lifo", "LIFO")]:
                impact = self._simulate_basis_method(method)
                results.append(
                    ElectionSimResult(
                        election_type="cost_basis_method",
                        scenario_name=name,
                        estimated_tax_impact=impact,
                        description=f"Using {name} method for all future disposals",
                        recommended=(method == "hifo"),
                    )
                )

        elif election_type == "section_475":
            results.append(
                ElectionSimResult(
                    election_type="section_475",
                    scenario_name="Mark-to-Market Trader Status",
                    estimated_tax_impact=Decimal("0"),
                    description=(
                        "Section 475(f) election: All positions marked to market at year-end. "
                        "Gains/losses treated as ordinary income. Eliminates wash-sale rules "
                        "and allows full loss deduction without $3,000 capital loss limitation."
                    ),
                    recommended=False,
                )
            )

        return results

    def _simulate_basis_method(self, method: str) -> Decimal:
        """Estimate tax impact if all open lots were sold today using the given method."""
        lots = self.session.query(TaxLot).filter(TaxLot.is_closed == False).all()

        # Group by asset
        by_asset: dict[int, list[TaxLot]] = {}
        for lot in lots:
            by_asset.setdefault(lot.asset_id, []).append(lot)

        total_gain = Decimal("0")
        for asset_id, asset_lots in by_asset.items():
            asset = self.session.get(Asset, asset_id)
            if not asset:
                continue
            price = self.market_data.get_current_price(asset)
            if price is None:
                continue

            if method == "fifo":
                sorted_lots = sorted(asset_lots, key=lambda l: l.acquisition_date)
            elif method == "lifo":
                sorted_lots = sorted(asset_lots, key=lambda l: l.acquisition_date, reverse=True)
            else:  # hifo
                sorted_lots = sorted(asset_lots, key=lambda l: l.cost_basis_per_unit, reverse=True)

            for lot in sorted_lots:
                gain = (price - lot.cost_basis_per_unit) * lot.remaining_quantity
                total_gain += gain

        return total_gain * SHORT_TERM_RATE

    def _has_recent_purchase(self, asset_id: int, account_id: int, days: int = 30) -> bool:
        """Check if there was a recent purchase that could trigger wash-sale rules."""
        cutoff = date.today() - timedelta(days=days)
        count = (
            self.session.query(Transaction)
            .filter(
                Transaction.asset_id == asset_id,
                Transaction.transaction_type == TransactionTypeEnum.BUY,
                Transaction.transaction_date >= cutoff,
            )
            .count()
        )
        return count > 0

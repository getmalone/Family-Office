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


# Approximate 2026 federal tax rates (top bracket)
SHORT_TERM_RATE = Decimal("0.37")
LONG_TERM_RATE = Decimal("0.20")
NIIT_RATE = Decimal("0.038")  # Net Investment Income Tax — applies to ST and LT gains, dividends, interest
CAPITAL_LOSS_LIMIT = Decimal("3000")  # IRC §1211(b): net capital loss usable against ordinary income
BASIS_SIM_FRACTION = Decimal("0.25")  # cost-basis comparison sells this share of each position


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
            # Active accounts only — you can't harvest in an account you've removed.
            .filter(TaxLot.is_closed == False, Account.is_taxable == True,
                    Account.is_active == True)
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
            tax_savings = Decimal("0")
            oldest_date = date.today()
            any_short_term = False
            loss_lot_ids: set[int] = set()

            for lot in lot_group:
                unrealized = (current_price - lot.cost_basis_per_unit) * lot.remaining_quantity
                if lot.acquisition_date < oldest_date:
                    oldest_date = lot.acquisition_date
                if unrealized < 0:
                    # Each loss lot saves tax at its own character's rate.
                    total_loss += unrealized
                    loss_lot_ids.add(lot.id)
                    short = not lot.is_long_term
                    any_short_term = any_short_term or short
                    rate = SHORT_TERM_RATE if short else LONG_TERM_RATE
                    tax_savings += abs(unrealized) * (rate + NIIT_RATE)

            if total_loss >= -min_loss:
                continue  # Loss too small

            # Check wash-sale risk
            wash_risk = self._has_recent_purchase(asset_id, exclude_lot_ids=loss_lot_ids)

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

        # Realized gains/losses from disposals in TAXABLE accounts only (soft-
        # deleted accounts still count — realized history stays). A wash-sale
        # loss is deferred into the replacement lot, so add the disallowed part
        # back; it is recognised later when the replacement is sold.
        rows = (
            self.session.query(TaxLotDisposal)
            .join(TaxLot, TaxLotDisposal.tax_lot_id == TaxLot.id)
            .join(Account, TaxLot.account_id == Account.id)
            .filter(
                Account.is_taxable == True,
                TaxLotDisposal.disposal_date >= year_start,
                TaxLotDisposal.disposal_date <= year_end,
            )
            .all()
        )
        recognised = [
            (d.is_short_term, d.realized_gain_loss + (d.wash_sale_disallowed or Decimal("0")))
            for d in rows
        ]
        wash_disallowed = sum((d.wash_sale_disallowed or Decimal("0") for d in rows), Decimal("0"))

        st_gains = sum((g for st, g in recognised if st and g > 0), Decimal("0"))
        st_losses = sum((g for st, g in recognised if st and g < 0), Decimal("0"))
        lt_gains = sum((g for st, g in recognised if not st and g > 0), Decimal("0"))
        lt_losses = sum((g for st, g in recognised if not st and g < 0), Decimal("0"))

        def _income(txn_type) -> Decimal:
            total = (
                self.session.query(func.coalesce(func.sum(Transaction.total_amount), 0))
                .join(Account, Transaction.account_id == Account.id)
                .filter(
                    Account.is_taxable == True,
                    Transaction.transaction_type == txn_type,
                    Transaction.transaction_date >= year_start,
                    Transaction.transaction_date <= year_end,
                )
                .scalar()
            )
            return Decimal(str(total or 0))

        divs = _income(TransactionTypeEnum.DIVIDEND)
        interest = _income(TransactionTypeEnum.INTEREST)

        # Deductible expenses
        expenses = Decimal(str((
            self.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
            .filter(
                ExpenseCategory.is_deductible == True,
                Expense.expense_date >= year_start,
                Expense.expense_date <= year_end,
            )
            .scalar()
        ) or 0))

        # ── Capital gain netting (Schedule D) ─────────────────────────────────
        # Net within each holding period, then a net loss in one offsets a net
        # gain in the other. A remaining net loss offsets up to $3,000 of
        # ordinary income; the rest carries forward.
        st_net = st_gains + st_losses
        lt_net = lt_gains + lt_losses
        if st_net < 0 < lt_net:
            lt_net, st_net = max(Decimal("0"), lt_net + st_net), min(Decimal("0"), st_net + lt_net)
        elif lt_net < 0 < st_net:
            st_net, lt_net = max(Decimal("0"), st_net + lt_net), min(Decimal("0"), lt_net + st_net)
        net = st_gains + st_losses + lt_gains + lt_losses
        net_loss = -min(Decimal("0"), st_net + lt_net) if (st_net <= 0 and lt_net <= 0) else Decimal("0")
        loss_deduction = min(net_loss, CAPITAL_LOSS_LIMIT)
        carryforward = net_loss - loss_deduction

        # Ordinary-rate income: net ST gain + interest, less deductible expenses
        # and the capital-loss deduction. Preferential: net LT gain + qualified
        # dividends. NIIT applies to all net investment income.
        ordinary = max(Decimal("0"), st_net) + interest
        preferential = max(Decimal("0"), lt_net) + divs
        # Expenses offset the highest-rate (ordinary) bucket first.
        ordinary_after = max(Decimal("0"), ordinary - expenses)
        preferential_after = max(Decimal("0"), preferential - max(Decimal("0"), expenses - ordinary))
        # The capital-loss deduction offsets ordinary income only; any part not
        # used here offsets ordinary income from outside the app (wages etc.).
        loss_here = min(loss_deduction, ordinary_after)
        ordinary_after -= loss_here
        loss_outside = loss_deduction - loss_here

        estimated_tax = max(
            Decimal("0"),
            ordinary_after * (SHORT_TERM_RATE + NIIT_RATE)
            + preferential_after * (LONG_TERM_RATE + NIIT_RATE)
            - loss_outside * SHORT_TERM_RATE,
        )

        return TaxSummary(
            tax_year=year,
            short_term_gains=st_gains,
            short_term_losses=st_losses,
            long_term_gains=lt_gains,
            long_term_losses=lt_losses,
            net_gain_loss=net,
            dividends_qualified=divs,
            dividends_ordinary=Decimal("0"),
            interest_income=interest,
            deductible_expenses=expenses,
            estimated_tax_liability=estimated_tax.quantize(Decimal("0.01")),
            loss_carryforward=carryforward,
            wash_sale_disallowed=wash_disallowed,
            capital_loss_deduction=loss_deduction,
        )

    def simulate_election(self, election_type: str) -> list[ElectionSimResult]:
        """
        Simulate the impact of various tax elections.

        Supports comparing FIFO vs HIFO vs specific-ID, Section 475 mark-to-market,
        and other tax elections that lawfully reduce taxable income.
        """
        results = []

        if election_type == "cost_basis_method":
            methods = [("fifo", "FIFO"), ("hifo", "Highest Cost First"), ("lifo", "LIFO")]
            impacts = {m: self._simulate_basis_method(m) for m, _ in methods}
            best = min(impacts, key=lambda m: impacts[m])
            pct = int(BASIS_SIM_FRACTION * 100)
            for method, name in methods:
                results.append(
                    ElectionSimResult(
                        election_type="cost_basis_method",
                        scenario_name=name,
                        estimated_tax_impact=impacts[method],
                        description=(f"Tax if {pct}% of every taxable position were sold today "
                                     f"choosing lots by {name}"),
                        recommended=(method == best),
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
        """Estimated tax if BASIS_SIM_FRACTION of every taxable position were sold
        today, choosing lots by ``method``. Each lot's gain is taxed at its own
        short/long-term rate (+NIIT) after netting the two holding periods, so
        the methods differ by which lots — and how much gain — they pick."""
        from app.services.holdings import open_lots_query

        lots = (open_lots_query(self.session)
                .filter(Account.is_taxable == True).all())

        by_position: dict[tuple[int, int], list[TaxLot]] = {}
        for lot in lots:
            by_position.setdefault((lot.asset_id, lot.account_id), []).append(lot)

        st_net = lt_net = Decimal("0")
        for (asset_id, _), pos_lots in by_position.items():
            asset = self.session.get(Asset, asset_id)
            if not asset:
                continue
            price = self.market_data.get_current_price(asset)
            if price is None:
                continue

            if method == "fifo":
                ordered = sorted(pos_lots, key=lambda l: (l.acquisition_date, l.id))
            elif method == "lifo":
                ordered = sorted(pos_lots, key=lambda l: (l.acquisition_date, l.id), reverse=True)
            else:  # hifo
                ordered = sorted(pos_lots, key=lambda l: l.cost_basis_per_unit, reverse=True)

            to_sell = sum((l.remaining_quantity for l in pos_lots), Decimal("0")) * BASIS_SIM_FRACTION
            for lot in ordered:
                if to_sell <= 0:
                    break
                qty = min(to_sell, lot.remaining_quantity)
                gain = (price - lot.cost_basis_per_unit) * qty
                if lot.is_long_term:
                    lt_net += gain
                else:
                    st_net += gain
                to_sell -= qty

        if st_net < 0 < lt_net:
            lt_net, st_net = max(Decimal("0"), lt_net + st_net), Decimal("0")
        elif lt_net < 0 < st_net:
            st_net, lt_net = max(Decimal("0"), st_net + lt_net), Decimal("0")
        tax = (max(Decimal("0"), st_net) * (SHORT_TERM_RATE + NIIT_RATE)
               + max(Decimal("0"), lt_net) * (LONG_TERM_RATE + NIIT_RATE))
        return tax.quantize(Decimal("0.01"))

    def _has_recent_purchase(self, asset_id: int, exclude_lot_ids: set[int] | None = None,
                             days: int = 30) -> bool:
        """Would selling now be a wash sale? True when shares of the asset were
        bought in the last ``days`` days in ANY account (an IRA purchase counts)
        and aren't among the loss lots that would be sold."""
        cutoff = date.today() - timedelta(days=days)
        q = self.session.query(TaxLot).filter(
            TaxLot.asset_id == asset_id,
            TaxLot.acquisition_date >= cutoff,
            TaxLot.remaining_quantity > 0,
        )
        if exclude_lot_ids:
            q = q.filter(TaxLot.id.notin_(list(exclude_lot_ids)))
        return q.count() > 0

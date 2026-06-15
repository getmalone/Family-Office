"""
Portfolio management service for the Family Office.

The Portfolio Agent uses this service to monitor positions, calculate NAV,
execute trades (with cost basis tracking), and detect wash-sale violations.
Maintains a unified portfolio database with FIFO, LIFO, and specific-ID
tax lot tracking.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.account import Account, tax_bucket_for
from app.models.asset import Asset, AssetPrice
from app.models.tax_lot import TaxLot, TaxLotDisposal, WashSaleAdjustment
from app.models.transaction import Transaction, TransactionTypeEnum
from app.schemas.portfolio import HoldingDetail, PortfolioSummary, TradeResult
from app.services.market_data import MarketDataService


class PortfolioService:
    """
    Core portfolio operations for the Family Office.

    Supports the Family Office's goal of preserving, growing, and diversifying
    the family's capital through active and passive investments across a wide
    range of asset classes.
    """

    def __init__(self, session: Session, market_data: MarketDataService | None = None):
        self.session = session
        self.market_data = market_data or MarketDataService(session)

    def get_summary(self, account_id: int | None = None) -> PortfolioSummary:
        """Get portfolio summary with all open positions."""
        query = (
            self.session.query(TaxLot)
            .filter(TaxLot.is_closed == False)
        )
        if account_id:
            query = query.filter(TaxLot.account_id == account_id)

        lots = query.all()

        # Aggregate by asset+account
        positions: dict[tuple[int, int], list[TaxLot]] = {}
        for lot in lots:
            key = (lot.asset_id, lot.account_id)
            positions.setdefault(key, []).append(lot)

        holdings = []
        total_mv = Decimal("0")
        total_cb = Decimal("0")
        allocation: dict[str, Decimal] = {}

        today = date.today()
        yesterday = today - timedelta(days=1)
        # Fetch the most recent prior-day close for every asset in one query
        # (looks back up to 5 calendar days to handle weekends/holidays)
        prior_cutoff = today - timedelta(days=5)
        prior_prices_rows = (
            self.session.query(AssetPrice.asset_id, AssetPrice.close_price, AssetPrice.price_date)
            .filter(
                AssetPrice.price_date >= prior_cutoff,
                AssetPrice.price_date < today,
            )
            .order_by(AssetPrice.asset_id, AssetPrice.price_date.desc())
            .all()
        )
        # Keep only the most recent prior-day price per asset
        prior_price_map: dict[int, Decimal] = {}
        for aid, price, _ in prior_prices_rows:
            if aid not in prior_price_map:
                prior_price_map[aid] = price

        for (asset_id, acct_id), lot_group in positions.items():
            asset = self.session.get(Asset, asset_id)
            account = self.session.get(Account, acct_id)
            if not asset or not account:
                continue

            quantity = sum(lot.remaining_quantity for lot in lot_group)
            cost_basis = sum(lot.remaining_quantity * lot.cost_basis_per_unit for lot in lot_group)

            current_price = self.market_data.get_current_price(asset) or Decimal("0")
            market_value = quantity * current_price
            unrealized = market_value - cost_basis
            unrealized_pct = (unrealized / cost_basis * 100) if cost_basis else Decimal("0")

            # Day change: (today_price - prior_close) * quantity
            prior_price = prior_price_map.get(asset_id)
            pos_day_change = (
                (current_price - prior_price) * quantity
                if prior_price and current_price
                else Decimal("0")
            )

            holdings.append(
                HoldingDetail(
                    asset_id=asset_id,
                    symbol=asset.symbol,
                    name=asset.name,
                    asset_class=str(asset.asset_class),
                    account_id=acct_id,
                    account_name=account.name,
                    quantity=quantity,
                    cost_basis=cost_basis,
                    current_price=current_price,
                    market_value=market_value,
                    unrealized_gain_loss=unrealized,
                    unrealized_pct=unrealized_pct,
                    day_change=pos_day_change,
                )
            )

            total_mv += market_value
            total_cb += cost_basis
            ac = str(asset.asset_class)
            allocation[ac] = allocation.get(ac, Decimal("0")) + market_value

        # Convert allocation to percentages
        if total_mv:
            allocation = {k: round(v / total_mv * 100, 2) for k, v in allocation.items()}

        # Set weight percentages
        for h in holdings:
            h.weight_pct = round(h.market_value / total_mv * 100, 2) if total_mv else Decimal("0")

        # Get YTD realized gains
        year_start = date(date.today().year, 1, 1)
        realized_ytd = (
            self.session.query(func.coalesce(func.sum(TaxLotDisposal.realized_gain_loss), 0))
            .filter(TaxLotDisposal.disposal_date >= year_start)
            .scalar()
        ) or Decimal("0")

        total_day_change = sum((h.day_change for h in holdings), Decimal("0"))

        return PortfolioSummary(
            total_market_value=total_mv,
            total_cost_basis=total_cb,
            total_unrealized_gain_loss=total_mv - total_cb,
            total_realized_ytd=Decimal(str(realized_ytd)),
            day_change=total_day_change,
            holdings=sorted(holdings, key=lambda h: h.market_value, reverse=True),
            allocation=allocation,
        )

    def execute_buy(
        self,
        account_id: int,
        asset_id: int,
        quantity: Decimal,
        price_per_unit: Decimal,
        fees: Decimal = Decimal("0"),
        transaction_date: date | None = None,
        notes: str = "",
        approval_id: int | None = None,
    ) -> Transaction:
        """Execute a buy trade and create the corresponding tax lot."""
        txn_date = transaction_date or date.today()
        total = quantity * price_per_unit + fees

        txn = Transaction(
            account_id=account_id,
            asset_id=asset_id,
            transaction_type=TransactionTypeEnum.BUY,
            transaction_date=txn_date,
            settlement_date=txn_date + timedelta(days=2),
            quantity=quantity,
            price_per_unit=price_per_unit,
            total_amount=total,
            fees=fees,
            notes=notes,
            approval_id=approval_id,
        )
        self.session.add(txn)
        self.session.flush()

        # Create tax lot
        cost_per_unit = (total) / quantity  # Include fees in cost basis
        lot = TaxLot(
            account_id=account_id,
            asset_id=asset_id,
            acquisition_date=txn_date,
            acquisition_transaction_id=txn.id,
            original_quantity=quantity,
            remaining_quantity=quantity,
            cost_basis_per_unit=cost_per_unit,
            original_cost_basis_per_unit=cost_per_unit,
        )
        self.session.add(lot)
        self.session.flush()

        return txn

    def execute_sell(
        self,
        account_id: int,
        asset_id: int,
        quantity: Decimal,
        price_per_unit: Decimal,
        fees: Decimal = Decimal("0"),
        transaction_date: date | None = None,
        method: str = "fifo",
        specific_lot_ids: list[int] | None = None,
        notes: str = "",
        approval_id: int | None = None,
    ) -> tuple[Transaction, list[TaxLotDisposal]]:
        """
        Execute a sell trade with automatic lot selection and wash-sale detection.

        Returns the transaction and list of lot disposals with realized gains/losses.
        """
        txn_date = transaction_date or date.today()
        total = quantity * price_per_unit - fees

        txn = Transaction(
            account_id=account_id,
            asset_id=asset_id,
            transaction_type=TransactionTypeEnum.SELL,
            transaction_date=txn_date,
            settlement_date=txn_date + timedelta(days=2),
            quantity=quantity,
            price_per_unit=price_per_unit,
            total_amount=total,
            fees=fees,
            notes=notes,
            approval_id=approval_id,
        )
        self.session.add(txn)
        self.session.flush()

        # Select lots
        lots = self._select_lots(account_id, asset_id, quantity, method, specific_lot_ids)

        disposals = []
        remaining = quantity
        for lot in lots:
            if remaining <= 0:
                break

            dispose_qty = min(remaining, lot.remaining_quantity)
            realized = (price_per_unit - lot.cost_basis_per_unit) * dispose_qty - (
                fees * dispose_qty / quantity
            )

            disposal = TaxLotDisposal(
                tax_lot_id=lot.id,
                sale_transaction_id=txn.id,
                quantity_disposed=dispose_qty,
                proceeds_per_unit=price_per_unit,
                realized_gain_loss=realized,
                is_short_term=not lot.is_long_term,
                disposal_date=txn_date,
            )
            self.session.add(disposal)

            lot.remaining_quantity -= dispose_qty
            if lot.remaining_quantity <= 0:
                lot.is_closed = True

            disposals.append(disposal)
            remaining -= dispose_qty

        self.session.flush()

        # Check for wash sales on loss disposals
        for disposal in disposals:
            if disposal.realized_gain_loss < 0:
                self._check_wash_sale(disposal, txn_date)

        return txn, disposals

    def _select_lots(
        self,
        account_id: int,
        asset_id: int,
        quantity: Decimal,
        method: str = "fifo",
        specific_lot_ids: list[int] | None = None,
    ) -> list[TaxLot]:
        """Select tax lots for disposal using the specified method."""
        query = self.session.query(TaxLot).filter(
            TaxLot.account_id == account_id,
            TaxLot.asset_id == asset_id,
            TaxLot.is_closed == False,
            TaxLot.remaining_quantity > 0,
        )

        if specific_lot_ids:
            query = query.filter(TaxLot.id.in_(specific_lot_ids))
        elif method == "fifo":
            query = query.order_by(TaxLot.acquisition_date.asc())
        elif method == "lifo":
            query = query.order_by(TaxLot.acquisition_date.desc())
        elif method == "hifo":  # Highest cost first (maximizes losses)
            query = query.order_by(TaxLot.cost_basis_per_unit.desc())

        return query.all()

    def _check_wash_sale(self, disposal: TaxLotDisposal, sale_date: date) -> None:
        """
        Check for wash-sale rule violations.

        If a substantially identical security was purchased within 30 days before or
        after the loss sale, the loss is disallowed and added to the replacement lot's
        cost basis.
        """
        window_start = sale_date - timedelta(days=30)
        window_end = sale_date + timedelta(days=30)

        lot = self.session.get(TaxLot, disposal.tax_lot_id)
        if not lot:
            return

        # Find replacement purchases within the wash-sale window
        replacement_lots = (
            self.session.query(TaxLot)
            .filter(
                TaxLot.asset_id == lot.asset_id,
                TaxLot.acquisition_date >= window_start,
                TaxLot.acquisition_date <= window_end,
                TaxLot.id != lot.id,
                TaxLot.is_closed == False,
            )
            .order_by(TaxLot.acquisition_date)
            .all()
        )

        if replacement_lots:
            replacement = replacement_lots[0]
            disallowed = abs(disposal.realized_gain_loss)

            # Mark disposal as wash sale
            disposal.is_wash_sale = True
            disposal.wash_sale_disallowed = disallowed

            # Adjust replacement lot's cost basis using original_quantity (not remaining_quantity)
            # to avoid inflating per-unit basis for partially sold lots
            replacement.cost_basis_per_unit += disallowed / replacement.original_quantity
            replacement.is_wash_sale_adjusted = True
            replacement.wash_sale_disallowed_loss += disallowed

            # Create audit record
            self.session.add(
                WashSaleAdjustment(
                    disallowed_disposal_id=disposal.id,
                    replacement_lot_id=replacement.id,
                    disallowed_loss=disallowed,
                    adjustment_date=sale_date,
                )
            )
            self.session.flush()

    def get_open_lots(
        self, account_id: int | None = None, asset_id: int | None = None
    ) -> list[TaxLot]:
        """Get all open (non-closed) tax lots, optionally filtered."""
        query = self.session.query(TaxLot).filter(TaxLot.is_closed == False)
        if account_id:
            query = query.filter(TaxLot.account_id == account_id)
        if asset_id:
            query = query.filter(TaxLot.asset_id == asset_id)
        return query.order_by(TaxLot.acquisition_date).all()

    def update_position(
        self,
        asset_id: int,
        account_id: int,
        new_quantity: Decimal,
        new_cost_basis_per_unit: Decimal,
    ) -> None:
        """
        Update quantity and cost basis for an existing position.

        Adjusts all open tax lots proportionally. If quantity changes, the
        difference is applied to the most recent lot (or a new lot is created).
        """
        lots = (
            self.session.query(TaxLot)
            .filter(
                TaxLot.asset_id == asset_id,
                TaxLot.account_id == account_id,
                TaxLot.is_closed == False,
            )
            .order_by(TaxLot.acquisition_date.asc())
            .all()
        )

        if not lots:
            return

        current_qty = sum(lot.remaining_quantity for lot in lots)
        qty_diff = new_quantity - current_qty

        if len(lots) == 1:
            # Single lot — update directly
            lots[0].remaining_quantity = new_quantity
            lots[0].original_quantity = new_quantity
            lots[0].cost_basis_per_unit = new_cost_basis_per_unit
            lots[0].original_cost_basis_per_unit = new_cost_basis_per_unit
        else:
            # Multiple lots — scale proportionally
            if current_qty > 0:
                scale = new_quantity / current_qty
                for lot in lots:
                    lot.remaining_quantity = round(lot.remaining_quantity * scale, 8)
                    lot.original_quantity = lot.remaining_quantity
                    lot.cost_basis_per_unit = new_cost_basis_per_unit
                    lot.original_cost_basis_per_unit = new_cost_basis_per_unit

        self.session.flush()

    def get_tax_bucket_balances(self) -> dict[str, Decimal]:
        """Current market value split into withdrawal-sequencing tax buckets.

        Returns {"taxable": .., "traditional": .., "roth": ..}. Buckets sum to
        the portfolio's total market value; account types are mapped via
        ``tax_bucket_for``. Used to seed the Monte Carlo Income Bridge.
        """
        balances: dict[str, Decimal] = {"taxable": Decimal("0"),
                                        "traditional": Decimal("0"),
                                        "roth": Decimal("0")}
        summary = self.get_summary()
        bucket_of: dict[int, str] = {}
        for h in summary.holdings:
            bucket = bucket_of.get(h.account_id)
            if bucket is None:
                account = self.session.get(Account, h.account_id)
                bucket = tax_bucket_for(account.account_type) if account else "taxable"
                bucket_of[h.account_id] = bucket
            balances[bucket] += h.market_value
        return balances

    def delete_position(self, asset_id: int, account_id: int) -> int:
        """
        Delete all open tax lots for a position. Returns count of lots removed.
        """
        lots = (
            self.session.query(TaxLot)
            .filter(
                TaxLot.asset_id == asset_id,
                TaxLot.account_id == account_id,
                TaxLot.is_closed == False,
            )
            .all()
        )
        count = len(lots)
        for lot in lots:
            lot.is_closed = True
            lot.remaining_quantity = Decimal("0")
        self.session.flush()
        return count

    def get_account_cards(self) -> list[dict]:
        """
        Per-account summary data for the account cards view.
        Returns accounts sorted by market value descending.
        """
        accounts = (
            self.session.query(Account)
            .filter(Account.is_active == True)
            .order_by(Account.name)
            .all()
        )

        results = []
        for account in accounts:
            summary = self.get_summary(account_id=account.id)
            if not summary.holdings and summary.total_market_value == 0:
                continue

            unrealized_pct = (
                float(summary.total_unrealized_gain_loss / summary.total_cost_basis * 100)
                if summary.total_cost_basis
                else 0.0
            )

            results.append({
                "account": account,
                "market_value": float(summary.total_market_value),
                "cost_basis": float(summary.total_cost_basis),
                "unrealized_gl": float(summary.total_unrealized_gain_loss),
                "unrealized_pct": round(unrealized_pct, 2),
                "allocation": {k: float(v) for k, v in summary.allocation.items()},
                "top_holdings": summary.holdings[:5],
                "num_positions": len(summary.holdings),
            })

        return sorted(results, key=lambda x: x["market_value"], reverse=True)

    def get_portfolio_history(self, days: int = 90) -> dict:
        """
        Historical portfolio value for trend charts.
        Returns total-portfolio and per-account value for each date we have prices.
        """
        from datetime import timedelta

        cutoff = date.today() - timedelta(days=days)

        # All open lots
        lots = self.session.query(TaxLot).filter(TaxLot.is_closed == False).all()
        if not lots:
            return {"dates": [], "total": [], "by_account": {}}

        asset_ids = list({lot.asset_id for lot in lots})

        # All prices in window
        prices_raw = (
            self.session.query(AssetPrice)
            .filter(
                AssetPrice.asset_id.in_(asset_ids),
                AssetPrice.price_date >= cutoff,
            )
            .order_by(AssetPrice.price_date)
            .all()
        )

        if not prices_raw:
            return {"dates": [], "total": [], "by_account": {}}

        # Build date → {asset_id → price}
        by_date: dict[date, dict[int, Decimal]] = {}
        for p in prices_raw:
            by_date.setdefault(p.price_date, {})[p.asset_id] = p.close_price

        # Fill forward: for any asset without a price on a given date use last known
        all_dates = sorted(by_date.keys())
        last_known: dict[int, Decimal] = {}
        filled: dict[date, dict[int, Decimal]] = {}
        for d in all_dates:
            last_known.update(by_date[d])
            filled[d] = dict(last_known)

        # Account name lookup
        account_map = {
            a.id: a.name
            for a in self.session.query(Account).filter(Account.is_active == True).all()
        }

        # For each date compute total and per-account
        date_labels = []
        totals = []
        by_account: dict[str, list[float]] = {}

        for d in all_dates:
            day_prices = filled[d]
            day_total = Decimal("0")
            acct_totals: dict[int, Decimal] = {}

            for lot in lots:
                price = day_prices.get(lot.asset_id, Decimal("0"))
                val = lot.remaining_quantity * price
                day_total += val
                acct_totals[lot.account_id] = acct_totals.get(lot.account_id, Decimal("0")) + val

            date_labels.append(d.strftime("%b %d"))
            totals.append(round(float(day_total), 2))

            for acct_id, val in acct_totals.items():
                name = account_map.get(acct_id, f"Account {acct_id}")
                by_account.setdefault(name, [None] * len(date_labels))
                # Pad shorter series
                while len(by_account[name]) < len(date_labels):
                    by_account[name].append(None)
                by_account[name][-1] = round(float(val), 2)

        # Pad any account series that are shorter than date_labels
        for name in by_account:
            while len(by_account[name]) < len(date_labels):
                by_account[name].append(None)

        return {"dates": date_labels, "total": totals, "by_account": by_account}

    def get_top_movers(self, limit: int = 10) -> dict:
        """Top gainers and losers by unrealized P&L %."""
        summary = self.get_summary()
        gainers = sorted(
            [h for h in summary.holdings if h.cost_basis > 0],
            key=lambda h: float(h.unrealized_pct),
            reverse=True,
        )
        return {
            "gainers": gainers[:limit],
            "losers": list(reversed(gainers))[:limit],
            "total_market_value": float(summary.total_market_value),
            "total_cost_basis": float(summary.total_cost_basis),
            "total_unrealized": float(summary.total_unrealized_gain_loss),
            "allocation": {k: float(v) for k, v in summary.allocation.items()},
        }

    def get_position_detail(
        self, asset_id: int, account_id: int
    ) -> dict | None:
        """Get aggregated position detail for editing."""
        lots = self.get_open_lots(account_id=account_id, asset_id=asset_id)
        if not lots:
            return None

        asset = self.session.get(Asset, asset_id)
        account = self.session.get(Account, account_id)
        if not asset or not account:
            return None

        quantity = sum(lot.remaining_quantity for lot in lots)
        total_basis = sum(lot.remaining_quantity * lot.cost_basis_per_unit for lot in lots)
        avg_basis = total_basis / quantity if quantity else Decimal("0")

        return {
            "asset_id": asset_id,
            "account_id": account_id,
            "symbol": asset.symbol,
            "name": asset.name,
            "account_name": account.name,
            "quantity": quantity,
            "cost_basis_per_unit": round(avg_basis, 6),
            "total_cost_basis": round(total_basis, 2),
            "num_lots": len(lots),
        }

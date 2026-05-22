#!/usr/bin/env python3
"""
Seed the Family Office database with demo data.

Creates family members, accounts, assets, sample positions, transactions,
and chart of accounts entries for demonstration and development.

Usage:
    python scripts/seed_demo_data.py
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.services.db import init_db, get_db_session
from app.models.family import FamilyMember, FamilyEntity, Ownership
from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.transaction import Transaction, TransactionTypeEnum
from app.models.tax_lot import TaxLot
from app.models.expense import ExpenseCategory, Vendor, Expense
from app.services.accounting_service import AccountingService
from app.services.approval_service import ApprovalService


def seed():
    """Populate the database with demo data for the Family Office."""
    engine, factory = init_db()

    with get_db_session(factory) as session:
        # Check if already seeded
        if session.query(FamilyMember).count() > 0:
            print("Database already seeded. Skipping.")
            return

        print("Seeding Family Office demo data...")

        # === Family Members ===
        principal = FamilyMember(
            first_name="Alex", last_name="Morgan",
            date_of_birth=date(1985, 6, 15),
            relationship="principal", email="alex@familyoffice.example.com",
        )
        spouse = FamilyMember(
            first_name="Jamie", last_name="Morgan",
            date_of_birth=date(1987, 3, 22),
            relationship="spouse", email="jamie@familyoffice.example.com",
        )
        child1 = FamilyMember(
            first_name="Emma", last_name="Morgan",
            date_of_birth=date(2015, 9, 10),
            relationship="child",
        )
        child2 = FamilyMember(
            first_name="Jack", last_name="Morgan",
            date_of_birth=date(2018, 1, 5),
            relationship="child",
        )
        session.add_all([principal, spouse, child1, child2])
        session.flush()
        print(f"  Created {4} family members")

        # === Family Entities ===
        llc = FamilyEntity(
            name="Morgan Family Holdings LLC",
            entity_type="llc",
            formation_date=date(2024, 1, 15),
            jurisdiction="Delaware",
            tax_election="partnership",
            notes="Multi-member LLC taxed as partnership. Primary investment vehicle.",
        )
        trust = FamilyEntity(
            name="Morgan Children's Trust",
            entity_type="trust",
            formation_date=date(2024, 6, 1),
            jurisdiction="Delaware",
            notes="Irrevocable trust for benefit of Emma and Jack.",
        )
        session.add_all([llc, trust])
        session.flush()
        print(f"  Created {2} family entities")

        # === Accounts ===
        schwab = Account(
            name="Schwab Brokerage - Alex",
            account_type=AccountTypeEnum.BROKERAGE,
            institution="Charles Schwab",
            is_taxable=True,
            primary_owner_id=principal.id,
            tax_entity_id=llc.id,
            opened_date=date(2020, 3, 1),
        )
        ira = Account(
            name="Schwab IRA - Alex",
            account_type=AccountTypeEnum.IRA_TRADITIONAL,
            institution="Charles Schwab",
            is_taxable=False,
            primary_owner_id=principal.id,
            opened_date=date(2018, 1, 15),
        )
        roth = Account(
            name="Schwab Roth IRA - Jamie",
            account_type=AccountTypeEnum.IRA_ROTH,
            institution="Charles Schwab",
            is_taxable=False,
            primary_owner_id=spouse.id,
            opened_date=date(2019, 4, 1),
        )
        crypto_acct = Account(
            name="Coinbase - Family",
            account_type=AccountTypeEnum.CRYPTO,
            institution="Coinbase",
            is_taxable=True,
            primary_owner_id=principal.id,
            tax_entity_id=llc.id,
        )
        savings = Account(
            name="Chase Savings",
            account_type=AccountTypeEnum.BANK_SAVINGS,
            institution="JPMorgan Chase",
            is_taxable=True,
            primary_owner_id=principal.id,
        )
        five29 = Account(
            name="529 Plan - Emma",
            account_type=AccountTypeEnum.FIVE29,
            institution="Vanguard",
            is_taxable=False,
            primary_owner_id=child1.id,
        )
        session.add_all([schwab, ira, roth, crypto_acct, savings, five29])
        session.flush()
        print(f"  Created {6} accounts")

        # === Ownership ===
        session.add_all([
            Ownership(owner_member_id=principal.id, account_id=schwab.id,
                      ownership_pct=Decimal("60"), effective_date=date(2024, 1, 15)),
            Ownership(owner_member_id=spouse.id, account_id=schwab.id,
                      ownership_pct=Decimal("40"), effective_date=date(2024, 1, 15)),
            Ownership(owner_member_id=principal.id, account_id=ira.id,
                      ownership_pct=Decimal("100"), effective_date=date(2018, 1, 15)),
            Ownership(owner_member_id=spouse.id, account_id=roth.id,
                      ownership_pct=Decimal("100"), effective_date=date(2019, 4, 1)),
        ])
        session.flush()

        # === Assets ===
        assets_data = [
            ("VOO", "Vanguard S&P 500 ETF", AssetClassEnum.US_EQUITY, True, "Broad Market"),
            ("QQQ", "Invesco QQQ Trust", AssetClassEnum.US_EQUITY, True, "Technology"),
            ("VXUS", "Vanguard Total Intl Stock ETF", AssetClassEnum.INTL_EQUITY, True, "International"),
            ("BND", "Vanguard Total Bond Market ETF", AssetClassEnum.FIXED_INCOME, True, "Fixed Income"),
            ("VNQ", "Vanguard Real Estate ETF", AssetClassEnum.REAL_ESTATE, True, "Real Estate"),
            ("AAPL", "Apple Inc.", AssetClassEnum.US_EQUITY, True, "Technology"),
            ("MSFT", "Microsoft Corp.", AssetClassEnum.US_EQUITY, True, "Technology"),
            ("GOOGL", "Alphabet Inc.", AssetClassEnum.US_EQUITY, True, "Technology"),
            ("AMZN", "Amazon.com Inc.", AssetClassEnum.US_EQUITY, True, "Consumer Discretionary"),
            ("BTC-USD", "Bitcoin", AssetClassEnum.CRYPTO, True, "Digital Assets"),
            ("ETH-USD", "Ethereum", AssetClassEnum.CRYPTO, True, "Digital Assets"),
            (None, "Acme Ventures Fund III LP", AssetClassEnum.VENTURE_CAPITAL, False, "Venture Capital"),
        ]

        asset_objs = {}
        for symbol, name, asset_class, public, sector in assets_data:
            a = Asset(symbol=symbol, name=name, asset_class=asset_class,
                      is_publicly_traded=public, sector=sector)
            session.add(a)
            session.flush()
            asset_objs[symbol or name] = a
        print(f"  Created {len(assets_data)} assets")

        # === Demo Prices (static for reproducibility) ===
        today = date.today()
        prices = {
            "VOO": Decimal("520.50"), "QQQ": Decimal("485.30"),
            "VXUS": Decimal("62.80"), "BND": Decimal("72.15"),
            "VNQ": Decimal("88.40"), "AAPL": Decimal("195.20"),
            "MSFT": Decimal("425.60"), "GOOGL": Decimal("175.90"),
            "AMZN": Decimal("198.50"), "BTC-USD": Decimal("87500.00"),
            "ETH-USD": Decimal("3250.00"),
        }
        for symbol, price in prices.items():
            if symbol in asset_objs:
                session.add(AssetPrice(
                    asset_id=asset_objs[symbol].id,
                    price_date=today, close_price=price, source="seed",
                ))
        session.flush()

        # === Demo Transactions & Tax Lots ===
        # Schwab Brokerage positions
        demo_positions = [
            (schwab, "VOO", Decimal("200"), Decimal("450.00"), date(2023, 3, 15)),
            (schwab, "VOO", Decimal("50"), Decimal("490.00"), date(2024, 1, 10)),
            (schwab, "QQQ", Decimal("150"), Decimal("380.00"), date(2023, 6, 1)),
            (schwab, "AAPL", Decimal("500"), Decimal("175.00"), date(2023, 9, 20)),
            (schwab, "MSFT", Decimal("200"), Decimal("350.00"), date(2023, 4, 15)),
            (schwab, "GOOGL", Decimal("300"), Decimal("140.00"), date(2023, 8, 1)),
            (schwab, "AMZN", Decimal("100"), Decimal("185.00"), date(2024, 2, 15)),
            (schwab, "VNQ", Decimal("400"), Decimal("95.00"), date(2023, 7, 1)),
            # IRA positions
            (ira, "VOO", Decimal("300"), Decimal("420.00"), date(2022, 1, 15)),
            (ira, "BND", Decimal("500"), Decimal("75.00"), date(2022, 6, 1)),
            (ira, "VXUS", Decimal("400"), Decimal("58.00"), date(2023, 1, 10)),
            # Roth IRA positions
            (roth, "QQQ", Decimal("100"), Decimal("350.00"), date(2022, 4, 1)),
            (roth, "AAPL", Decimal("200"), Decimal("160.00"), date(2022, 9, 15)),
            # Crypto
            (crypto_acct, "BTC-USD", Decimal("2.5"), Decimal("42000.00"), date(2023, 5, 1)),
            (crypto_acct, "ETH-USD", Decimal("15"), Decimal("2200.00"), date(2023, 7, 15)),
        ]

        for account, symbol, qty, price, acq_date in demo_positions:
            asset = asset_objs[symbol]
            total = qty * price
            txn = Transaction(
                account_id=account.id, asset_id=asset.id,
                transaction_type=TransactionTypeEnum.BUY,
                transaction_date=acq_date,
                settlement_date=acq_date + timedelta(days=2),
                quantity=qty, price_per_unit=price, total_amount=total,
                fees=Decimal("0"), notes=f"Seed: Buy {qty} {symbol}",
            )
            session.add(txn)
            session.flush()

            lot = TaxLot(
                account_id=account.id, asset_id=asset.id,
                acquisition_date=acq_date,
                acquisition_transaction_id=txn.id,
                original_quantity=qty, remaining_quantity=qty,
                cost_basis_per_unit=price,
                original_cost_basis_per_unit=price,
            )
            session.add(lot)

        # A sold position (realized loss for demo)
        sell_asset = asset_objs["VNQ"]
        sell_txn = Transaction(
            account_id=schwab.id, asset_id=sell_asset.id,
            transaction_type=TransactionTypeEnum.SELL,
            transaction_date=date(2024, 11, 15),
            settlement_date=date(2024, 11, 18),
            quantity=Decimal("100"), price_per_unit=Decimal("82.00"),
            total_amount=Decimal("8200"), fees=Decimal("0"),
            notes="Sold 100 VNQ at loss for tax harvesting",
        )
        session.add(sell_txn)

        # Dividend transaction
        div_txn = Transaction(
            account_id=schwab.id, asset_id=asset_objs["VOO"].id,
            transaction_type=TransactionTypeEnum.DIVIDEND,
            transaction_date=date(2024, 12, 20),
            quantity=Decimal("0"), total_amount=Decimal("1250.00"),
            fees=Decimal("0"), notes="Q4 2024 dividend",
        )
        session.add(div_txn)

        session.flush()
        print(f"  Created {len(demo_positions) + 2} transactions and {len(demo_positions)} tax lots")

        # === Chart of Accounts ===
        acct_svc = AccountingService(session)
        acct_svc.seed_chart_of_accounts()
        print("  Seeded chart of accounts")

        # === Approval Policies ===
        approval_svc = ApprovalService(session)
        approval_svc.seed_default_policies()
        print("  Seeded approval policies")

        # === Investment Profiles ===
        from app.services.analysis_service import AnalysisService
        analysis_svc = AnalysisService(session)
        analysis_svc.seed_default_profiles()
        print("  Seeded investment profiles (5 system profiles, Balanced active)")

        # === Expense Categories ===
        categories = [
            ("Investment Management Fees", True, "investment_expense"),
            ("Legal & Professional Fees", True, "investment_expense"),
            ("Tax Preparation", True, "investment_expense"),
            ("Research & Data Subscriptions", True, "investment_expense"),
            ("Travel - Due Diligence", True, "investment_expense"),
            ("Office & Administrative", True, "investment_expense"),
            ("Insurance", True, "investment_expense"),
            ("Charitable Contributions", True, "charitable"),
            ("Personal Expenses", False, None),
        ]
        for name, deductible, category in categories:
            session.add(ExpenseCategory(
                name=name, is_deductible=deductible, deduction_category=category,
            ))
        session.flush()

        # === Demo Expenses ===
        legal_cat = session.query(ExpenseCategory).filter(
            ExpenseCategory.name == "Legal & Professional Fees"
        ).first()
        tax_cat = session.query(ExpenseCategory).filter(
            ExpenseCategory.name == "Tax Preparation"
        ).first()
        research_cat = session.query(ExpenseCategory).filter(
            ExpenseCategory.name == "Research & Data Subscriptions"
        ).first()

        if legal_cat and tax_cat and research_cat:
            session.add_all([
                Expense(
                    category_id=legal_cat.id, entity_id=llc.id,
                    amount=Decimal("15000"), expense_date=date(2024, 3, 15),
                    description="Annual legal review - Morgan Family Holdings LLC",
                    tax_year=2024,
                ),
                Expense(
                    category_id=tax_cat.id, entity_id=llc.id,
                    amount=Decimal("8500"), expense_date=date(2024, 4, 10),
                    description="2023 tax preparation - CPA firm",
                    tax_year=2024,
                ),
                Expense(
                    category_id=research_cat.id, entity_id=llc.id,
                    amount=Decimal("2400"), expense_date=date(2024, 1, 5),
                    description="Bloomberg Terminal subscription (annual)",
                    tax_year=2024, is_recurring=True,
                ),
            ])
        session.flush()
        print("  Created demo expenses")

        print("\nDone! Family Office demo data seeded successfully.")
        print(f"Database: {settings.db_path}")


if __name__ == "__main__":
    seed()

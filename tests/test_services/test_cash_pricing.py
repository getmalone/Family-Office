"""Cash / stable-value is always $1.00 — never over-valued from a stored price.

Regression for the "CASH priced as the PGIM Ultra Short Bond ETF (~$83)" bug:
the write paths were fixed earlier, but the valuation lookup still trusted a
stale cached price. These lock in $1 at the source.
"""

from datetime import date
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.import_service import import_positions_csv
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService


def test_get_current_price_cash_is_one_despite_bad_row(session):
    cash = Asset(symbol="CASH", name="Cash", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    session.add(cash); session.flush()
    session.add(AssetPrice(asset_id=cash.id, price_date=date.today(), close_price=Decimal("83.03"), source="yfinance"))
    session.flush()
    assert MarketDataService(session).get_current_price(cash) == Decimal("1")


def test_money_market_ticker_is_one(session):
    mm = Asset(symbol="VMFXX", name="Vanguard Federal MM", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    session.add(mm); session.flush()
    session.add(AssetPrice(asset_id=mm.id, price_date=date.today(), close_price=Decimal("100.0"), source="yfinance"))
    session.flush()
    assert MarketDataService(session).get_current_price(mm) == Decimal("1")


def test_repair_only_touches_stable_value(session):
    cash = Asset(id=1, symbol="CASH", name="Cash", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    voo = Asset(id=2, symbol="VOO", name="Vanguard", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add_all([cash, voo]); session.flush()
    session.add(AssetPrice(asset_id=1, price_date=date.today(), close_price=Decimal("83.03"), source="x"))
    session.add(AssetPrice(asset_id=2, price_date=date.today(), close_price=Decimal("520"), source="x"))
    session.flush()

    fixed = MarketDataService(session).repair_stable_value_prices()
    assert fixed == 1
    assert session.query(AssetPrice).filter_by(asset_id=1).first().close_price == Decimal("1")
    assert session.query(AssetPrice).filter_by(asset_id=2).first().close_price == Decimal("520")  # untouched


def test_portfolio_values_cash_at_one(session):
    cash = Asset(id=1, symbol="CASH", name="Cash", asset_class=AssetClassEnum.CASH, is_publicly_traded=True)
    acct = Account(id=1, name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True)
    session.add_all([cash, acct]); session.flush()
    txn = Transaction(account_id=1, asset_id=1, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2023, 1, 1), quantity=Decimal("9300.95"), price_per_unit=Decimal("1"),
                      total_amount=Decimal("9300.95"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=1, asset_id=1, acquisition_date=date(2023, 1, 1), acquisition_transaction_id=txn.id,
                       original_quantity=Decimal("9300.95"), remaining_quantity=Decimal("9300.95"),
                       cost_basis_per_unit=Decimal("1"), original_cost_basis_per_unit=Decimal("1")))
    # Corrupt $83 row present — must be ignored.
    session.add(AssetPrice(asset_id=1, price_date=date.today(), close_price=Decimal("83.03"), source="yfinance"))
    session.flush()

    summary = PortfolioService(session).get_summary()
    assert summary.total_market_value == Decimal("9300.95")        # $1 × qty, not ~$772k


def test_import_pins_stable_value_to_one(session):
    # An import file that lists CASH with a bogus per-unit price must store $1.
    csv_text = (
        "account,symbol,name,asset_class,quantity,price,cost_basis_total\n"
        "Brokerage,CASH,Cash Sweep,cash,9300.95,83.03,9300.95\n"
    )
    import_positions_csv(session, csv_text)
    session.flush()
    cash = session.query(Asset).filter(Asset.symbol == "CASH").first()
    price = session.query(AssetPrice).filter_by(asset_id=cash.id).first()
    assert price.close_price == Decimal("1")

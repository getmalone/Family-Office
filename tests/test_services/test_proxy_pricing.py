"""Proxy pricing for untickered holdings (401k CITs).

A holding with no public ticker but a manual base price + a look_through_ticker
(a public fund that tracks its strategy) is valued as base × the proxy's return
since the base date — so it moves day-to-day instead of sitting frozen. All
DB-only: a render must never hit the network.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.market_data import MarketDataService


def _cit(session, aid, name, proxy=None, traded=False):
    a = Asset(id=aid, symbol=None, name=name, asset_class=AssetClassEnum.US_EQUITY,
              is_publicly_traded=traded, look_through_ticker=proxy)
    session.add(a); session.flush()
    return a


def _proxy_ref(session, aid, symbol, base_date, base_px, latest_px):
    ref = Asset(id=aid, symbol=symbol, name=f"(price proxy) {symbol}",
                asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True, is_reference=True)
    session.add(ref); session.flush()
    session.add(AssetPrice(asset_id=aid, price_date=base_date, close_price=Decimal(base_px), source="yfinance-backfill"))
    session.add(AssetPrice(asset_id=aid, price_date=date.today(), close_price=Decimal(latest_px), source="snapshot"))
    session.flush()
    return ref


def test_proxy_tracks_return_from_manual_base(session):
    cit = _cit(session, 1, "Fidelity Contrafund Pool", proxy="FCNTX")
    base_date = date.today() - timedelta(days=30)
    session.add(AssetPrice(asset_id=1, price_date=base_date, close_price=Decimal("20.00"), source="manual"))
    _proxy_ref(session, 2, "FCNTX", base_date, "100", "110")   # proxy up 10%
    session.flush()
    # 20.00 × (110 / 100) = 22.00
    assert MarketDataService(session).get_current_price(cit) == Decimal("22.000000")


def test_no_proxy_falls_back_to_manual(session):
    cit = _cit(session, 1, "Plan Pool, no proxy")
    session.add(AssetPrice(asset_id=1, price_date=date.today(), close_price=Decimal("9.94"), source="manual"))
    session.flush()
    assert MarketDataService(session).get_current_price(cit) == Decimal("9.94")


def test_proxy_without_history_falls_back_to_manual(session):
    cit = _cit(session, 1, "Pool with proxy but no proxy data", proxy="RFGTX")
    session.add(AssetPrice(asset_id=1, price_date=date.today(), close_price=Decimal("20.46"), source="manual"))
    session.flush()
    # No reference asset / no proxy prices yet → just the manual base, never network.
    assert MarketDataService(session).get_current_price(cit) == Decimal("20.46")


def test_proxy_symbols_for_held(session):
    # A held, untickered holding with a proxy → its proxy symbol is collected for backfill.
    cit = _cit(session, 1, "Capital Group 2040 TD", proxy="RFGTX")
    acct = Account(id=1, name="401k", account_type=AccountTypeEnum.FOUR01K, is_taxable=False)
    session.add(acct); session.flush()
    txn = Transaction(account_id=1, asset_id=1, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2023, 1, 1), quantity=Decimal("100"), price_per_unit=Decimal("20"),
                      total_amount=Decimal("2000"), fees=Decimal("0"))
    session.add(txn); session.flush()
    session.add(TaxLot(account_id=1, asset_id=1, acquisition_date=date(2023, 1, 1), acquisition_transaction_id=txn.id,
                       original_quantity=Decimal("100"), remaining_quantity=Decimal("100"),
                       cost_basis_per_unit=Decimal("20"), original_cost_basis_per_unit=Decimal("20")))
    session.flush()
    assert MarketDataService(session)._proxy_symbols_for_held() == {"RFGTX"}


def test_reference_assets_hidden_from_manage_list(test_client, engine):
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=engine)()
    s.add(Asset(symbol="FCNTX", name="(price proxy) FCNTX", asset_class=AssetClassEnum.US_EQUITY,
                is_publicly_traded=True, is_reference=True))
    s.add(Asset(symbol="AAPL", name="Apple Inc.", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True))
    s.commit()
    html = test_client.get("/portfolio/assets").text
    assert "AAPL" in html
    assert "(price proxy) FCNTX" not in html

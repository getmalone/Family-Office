"""Tests for the generic CSV importer and DB backup."""

from datetime import date
from decimal import Decimal

from app.config import Settings
from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.import_service import (
    detect_format,
    import_positions_csv,
    import_positions_data,
    IMPORT_NOTE,
)

CSV = (
    "account,account_type,symbol,name,asset_class,quantity,cost_basis_total,acquired,price\n"
    "My Brokerage,brokerage,VOO,Vanguard S&P 500,us_equity,100,45000,2023-03-15,520.50\n"
    "My Roth IRA,ira_roth,AAPL,Apple Inc.,us_equity,50,8000,2022-06-01,195.20\n"
)


def test_import_creates_accounts_assets_and_lots(session):
    r = import_positions_csv(session, CSV)
    assert (r["accounts"], r["assets"], r["positions"]) == (2, 2, 2)
    assert r["errors"] == []

    roth = session.query(Account).filter(Account.name == "My Roth IRA").first()
    assert roth.is_taxable is False  # tax-advantaged account flagged correctly

    lots = session.query(TaxLot).all()
    assert len(lots) == 2
    voo = session.query(Asset).filter(Asset.symbol == "VOO").first()
    voo_lot = next(l for l in lots if l.asset_id == voo.id)
    assert voo_lot.original_quantity == Decimal("100")
    assert voo_lot.cost_basis_per_unit == Decimal("450")  # 45000 / 100


def test_import_reuses_existing_account(session):
    import_positions_csv(session, CSV)
    # Re-importing into the same-named account should not create a duplicate.
    more = (
        "account,symbol,quantity,cost_per_share\n"
        "My Brokerage,MSFT,10,300\n"
    )
    r = import_positions_csv(session, more)
    assert r["accounts"] == 0  # reused
    assert session.query(Account).filter(Account.name == "My Brokerage").count() == 1


def test_import_skips_bad_rows(session):
    bad = "account,quantity\n,5\nGood,abc\nGood,10\n"
    r = import_positions_csv(session, bad)
    assert r["positions"] == 1
    assert len(r["errors"]) == 2


def test_detect_format():
    assert detect_format('{"a":1}') == "json"
    assert detect_format("<positions/>") == "xml"
    assert detect_format("account,quantity\nx,1") == "csv"
    assert detect_format("anything", "export.JSON") == "json"  # extension wins


def test_import_json_nested_accounts(session):
    raw = """
    {"accounts": [
      {"account": "JSON Brokerage", "account_type": "brokerage",
       "positions": [
         {"symbol": "VOO", "quantity": 100, "cost_basis_total": 45000, "price": 520.50},
         {"symbol": "BND", "quantity": 200, "cost_per_share": 72}
       ]}
    ]}
    """
    r = import_positions_data(session, raw, filename="x.json")
    assert r["format"] == "json"
    assert r["positions"] == 2
    acct = session.query(Account).filter(Account.name == "JSON Brokerage").first()
    assert acct is not None
    voo = session.query(Asset).filter(Asset.symbol == "VOO").first()
    lot = session.query(TaxLot).filter(TaxLot.asset_id == voo.id).first()
    assert lot.cost_basis_per_unit == Decimal("450")


def test_import_json_flat_list(session):
    raw = '[{"account":"Flat","symbol":"QQQ","quantity":10,"cost_basis_total":5000}]'
    r = import_positions_data(session, raw, filename="f.json")
    assert r["positions"] == 1


def test_import_xml_flat(session):
    raw = (
        '<positions>'
        '<position account="XML Brokerage" symbol="VTI" quantity="10" '
        'cost_basis_total="2500" price="260"/>'
        '</positions>'
    )
    r = import_positions_data(session, raw, filename="x.xml")
    assert r["format"] == "xml"
    assert r["positions"] == 1
    assert session.query(Account).filter(Account.name == "XML Brokerage").first() is not None


def test_import_xml_nested_accounts(session):
    raw = """
    <accounts>
      <account name="XML IRA" account_type="ira_roth">
        <positions>
          <position><symbol>AAPL</symbol><quantity>50</quantity><cost_basis_total>8000</cost_basis_total></position>
        </positions>
      </account>
    </accounts>
    """
    r = import_positions_data(session, raw, filename="x.xml")
    assert r["positions"] == 1
    ira = session.query(Account).filter(Account.name == "XML IRA").first()
    assert ira.is_taxable is False


def test_import_xml_rejects_dtd(session):
    raw = '<!DOCTYPE x [<!ENTITY a "b">]><positions></positions>'
    try:
        import_positions_data(session, raw, filename="x.xml")
        assert False, "should reject DTD/entities"
    except ValueError:
        pass


def test_backup_copies_database_file(tmp_path, monkeypatch):
    from app.config import settings as runtime
    from app.services import app_settings
    from app.services.db import create_db_engine, create_tables

    dbp = tmp_path / "fo.db"
    engine = create_db_engine(Settings(db_path=str(dbp)))
    create_tables(engine)
    engine.dispose()

    monkeypatch.setattr(runtime, "db_path", str(dbp))
    out = app_settings.backup_database()
    assert out.exists()
    assert out.parent.name == "backups"
    assert out.stat().st_size > 0


# A raw Google Sheets export: a blank first row, a blank leading column, and a
# blank row between blocks. Previously every row failed with "missing account".
SHEET_CSV = (
    ",,,,\n"
    ",account,account_type,symbol,quantity\n"
    ",SALESFORCE.COM,401k,84679P173,418\n"
    ",,,,\n"
    ",BrokerageLink,brokerage,SCHD,425\n"
)


def test_import_tolerates_spreadsheet_export_shape(session):
    r = import_positions_csv(session, SHEET_CSV)
    assert (r["accounts"], r["positions"], r["errors"]) == (2, 2, [])
    assert session.query(Asset).filter(Asset.symbol == "84679P173").count() == 1


def test_import_maps_account_and_asset_class_aliases(session):
    csv_text = (
        "account,account_type,symbol,asset_class,quantity\n"
        "Work 401k,401(k),SPINTL,international_equity,10\n"
        "Rollover,Traditional IRA,BND,Bond,20\n"
    )
    r = import_positions_csv(session, csv_text)
    assert r["errors"] == []
    assert session.query(Account).filter(Account.name == "Work 401k").first().account_type == AccountTypeEnum.FOUR01K
    assert session.query(Account).filter(Account.name == "Rollover").first().account_type == AccountTypeEnum.IRA_TRADITIONAL
    assert session.query(Asset).filter(Asset.symbol == "SPINTL").first().asset_class == AssetClassEnum.INTL_EQUITY
    assert session.query(Asset).filter(Asset.symbol == "BND").first().asset_class == AssetClassEnum.FIXED_INCOME


def _lots_for(session, symbol):
    asset = session.query(Asset).filter(Asset.symbol == symbol).first()
    return session.query(TaxLot).filter(TaxLot.asset_id == asset.id).all()


def test_reimport_replaces_rather_than_stacking(session):
    import_positions_csv(session, CSV)
    r = import_positions_csv(session, CSV)
    # Same file twice must leave the same holdings, not double them.
    assert (r["accounts"], r["positions"], r["replaced"], r["kept"]) == (0, 2, 2, 0)
    assert len(_lots_for(session, "VOO")) == 1
    assert session.query(TaxLot).count() == 2
    assert session.query(Transaction).count() == 2


def test_reimport_applies_updated_quantities(session):
    import_positions_csv(session, CSV)
    updated = (
        "account,account_type,symbol,name,asset_class,quantity,cost_basis_total,acquired,price\n"
        "My Brokerage,brokerage,VOO,Vanguard S&P 500,us_equity,140,63000,2023-03-15,520.50\n"
    )
    import_positions_csv(session, updated)
    lots = _lots_for(session, "VOO")
    assert len(lots) == 1
    assert lots[0].original_quantity == Decimal("140")
    # An account absent from the second file keeps its positions.
    assert len(_lots_for(session, "AAPL")) == 1


def test_reimport_heals_already_duplicated_positions(session):
    import_positions_csv(session, CSV)
    # Reproduce the damage the old append-only importer left behind: two extra
    # stacked copies of every imported lot.
    originals = session.query(TaxLot).all()
    for _ in range(2):
        for lot in originals:
            txn = Transaction(
                account_id=lot.account_id, asset_id=lot.asset_id,
                transaction_type=TransactionTypeEnum.BUY,
                transaction_date=lot.acquisition_date,
                quantity=lot.original_quantity,
                price_per_unit=lot.cost_basis_per_unit,
                total_amount=lot.original_quantity * lot.cost_basis_per_unit,
                notes=IMPORT_NOTE,
            )
            session.add(txn)
            session.flush()
            session.add(TaxLot(
                account_id=lot.account_id, asset_id=lot.asset_id,
                acquisition_date=lot.acquisition_date,
                acquisition_transaction_id=txn.id,
                original_quantity=lot.original_quantity,
                remaining_quantity=lot.remaining_quantity,
                cost_basis_per_unit=lot.cost_basis_per_unit,
                original_cost_basis_per_unit=lot.cost_basis_per_unit,
                cost_basis_method="fifo", is_closed=False,
            ))
        session.flush()
    assert session.query(TaxLot).count() == 6  # 3x inflated
    assert len(_lots_for(session, "VOO")) == 3

    r = import_positions_csv(session, CSV)
    assert r["replaced"] == 6  # every stacked copy cleared
    assert session.query(TaxLot).count() == 2
    assert len(_lots_for(session, "VOO")) == 1


def test_reimport_preserves_sold_and_manual_positions(session):
    import_positions_csv(session, CSV)
    voo_lot = _lots_for(session, "VOO")[0]
    voo_lot.remaining_quantity = Decimal("60")  # 40 shares sold since the import
    account_id = voo_lot.account_id
    asset_id = voo_lot.asset_id
    manual = Transaction(
        account_id=account_id, asset_id=asset_id,
        transaction_type=TransactionTypeEnum.BUY, transaction_date=date(2024, 1, 5),
        quantity=Decimal("7"), price_per_unit=Decimal("500"),
        total_amount=Decimal("3500"), notes="Entered by hand",
    )
    session.add(manual)
    session.flush()
    session.add(TaxLot(
        account_id=account_id, asset_id=asset_id, acquisition_date=date(2024, 1, 5),
        acquisition_transaction_id=manual.id, original_quantity=Decimal("7"),
        remaining_quantity=Decimal("7"), cost_basis_per_unit=Decimal("500"),
        original_cost_basis_per_unit=Decimal("500"), cost_basis_method="fifo",
        is_closed=False,
    ))
    session.flush()

    r = import_positions_csv(session, CSV)
    assert r["kept"] == 1  # the partially sold lot was left alone
    lots = _lots_for(session, "VOO")
    quantities = sorted(l.remaining_quantity for l in lots)
    # partially sold (60) + hand-entered (7) + the fresh import (100)
    assert quantities == [Decimal("7"), Decimal("60"), Decimal("100")]
    assert session.query(Transaction).filter(Transaction.notes == "Entered by hand").count() == 1

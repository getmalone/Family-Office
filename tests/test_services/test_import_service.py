"""Tests for the generic CSV importer and DB backup."""

from decimal import Decimal

from app.config import Settings
from app.models.account import Account
from app.models.asset import Asset
from app.models.tax_lot import TaxLot
from app.services.import_service import (
    detect_format,
    import_positions_csv,
    import_positions_data,
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

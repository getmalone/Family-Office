"""Tests for the Settings page (config + CSV import) routes."""


def test_settings_page_loads(test_client):
    r = test_client.get("/settings/")
    assert r.status_code == 200
    assert "Settings" in r.text


def test_save_api_key_persists_and_applies(test_client):
    r = test_client.post(
        "/settings/",
        data={"anthropic_api_key": "sk-test-123", "llm_model": "claude-test"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    from app.config import settings
    assert settings.anthropic_api_key == "sk-test-123"
    assert settings.llm_model == "claude-test"

    # Key is shown only as "configured", never echoed back.
    page = test_client.get("/settings/").text
    assert "configured" in page
    assert "sk-test-123" not in page


def test_csv_import_via_upload(test_client):
    csv_bytes = (
        b"account,symbol,quantity,cost_basis_total,price\n"
        b"Test Brokerage,VTI,10,2500,260\n"
    )
    r = test_client.post(
        "/settings/import",
        files={"file": ("positions.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Imported" in r.headers["location"]


# The exact shape of a raw Google Sheets export: blank first row, blank leading
# column. Uploaded twice, it must not double the account's holdings.
SHEET_EXPORT = (
    b",,,,,\n"
    b",account,account_type,symbol,asset_class,quantity\n"
    b",SALESFORCE.COM,401k,84679P140,international_equity,146\n"
    b",BrokerageLink,brokerage,SCHD,us_equity,425\n"
)


def test_reupload_does_not_inflate_holdings(test_client, session):
    from app.models.tax_lot import TaxLot

    def upload():
        return test_client.post(
            "/settings/import",
            files={"file": ("sheet.csv", SHEET_EXPORT, "text/csv")},
            follow_redirects=False,
        )

    first = upload()
    assert first.status_code == 303
    assert session.query(TaxLot).count() == 2

    second = upload()
    assert second.status_code == 303
    assert "Replaced+2" in second.headers["location"]
    assert session.query(TaxLot).count() == 2  # not 4
    quantities = sorted(l.remaining_quantity for l in session.query(TaxLot).all())
    assert [str(q) for q in quantities] == ["146.00000000", "425.00000000"]


def test_settings_offers_and_applies_duplicate_cleanup(test_client, session, monkeypatch):
    """The stacked-import panel shows up with real counts, and the button
    removes the superseded uploads after taking a backup."""
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    from app.models.tax_lot import TaxLot
    from app.services import app_settings
    from tests.test_services.test_import_repair import _account, _asset, _upload

    acct = _account(session, "BrokerageLink")
    voo = _asset(session, "VOO")
    base = datetime(2026, 8, 1, 9, 0, 0)
    _upload(session, acct, [(voo, "425", date(2023, 3, 15))], base)
    _upload(session, acct, [(voo, "450", date(2023, 3, 15))], base + timedelta(days=30))
    session.commit()

    page = test_client.get("/settings/").text
    assert "Duplicate positions found" in page
    assert "BrokerageLink" in page

    backups = []
    monkeypatch.setattr(app_settings, "backup_database", lambda: backups.append(1))

    r = test_client.post("/settings/repair-duplicates", follow_redirects=False)
    assert r.status_code == 303
    assert "Removed+1+duplicate" in r.headers["location"]
    assert backups  # never mutate without a backup first

    lots = session.query(TaxLot).all()
    assert len(lots) == 1
    assert lots[0].original_quantity == Decimal("450")
    assert "Duplicate positions found" not in test_client.get("/settings/").text


def test_accounts_page_offers_activate_and_permanent_delete(test_client, session, monkeypatch):
    """An inactive account must be recoverable and, failing that, removable —
    previously it could only be edited, with no way back and no way out."""
    from app.models.account import Account
    from app.models.tax_lot import TaxLot
    from app.services import app_settings
    from app.services.import_service import import_positions_csv

    import_positions_csv(session, "account,symbol,quantity\nOld 401k,VTI,10\n")
    acct = session.query(Account).filter(Account.name == "Old 401k").first()
    acct.is_active = False
    session.commit()

    page = test_client.get("/portfolio/accounts").text
    assert "/portfolio/accounts/activate/%d" % acct.id in page
    assert "/portfolio/accounts/purge/%d" % acct.id in page
    assert "Added" in page and "Updated" in page  # date columns

    # Reactivate, and the destructive control disappears again.
    r = test_client.post(f"/portfolio/accounts/activate/{acct.id}", follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Account, acct.id).is_active is True
    assert "/portfolio/accounts/purge/%d" % acct.id not in test_client.get("/portfolio/accounts").text

    # An active account cannot be purged.
    r = test_client.post(f"/portfolio/accounts/purge/{acct.id}", follow_redirects=False)
    assert "err=Deactivate" in r.headers["location"]
    assert session.get(Account, acct.id) is not None

    backups = []
    monkeypatch.setattr(app_settings, "backup_database", lambda: backups.append(1))
    test_client.post(f"/portfolio/accounts/delete/{acct.id}", follow_redirects=False)
    r = test_client.post(f"/portfolio/accounts/purge/{acct.id}", follow_redirects=False)
    assert "msg=Deleted+Old+401k+permanently" in r.headers["location"]
    assert backups
    session.commit()  # end this session's read snapshot before re-checking
    assert session.query(Account).filter(Account.id == acct.id).first() is None
    assert session.query(TaxLot).count() == 0


def test_upload_update_endpoint_reports_the_result(test_client, monkeypatch):
    from app.services import updater

    monkeypatch.setattr(updater, "apply_bundle_file",
                        lambda data: (True, f"updated to v9.9.9 ({len(data)} bytes)"))
    r = test_client.post("/settings/upload-update",
                         files={"file": ("family-office-9.9.9.zip", b"PK\x03\x04zip",
                                         "application/zip")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "updated to v9.9.9 (7 bytes)" in body["message"]
    assert r.headers["cache-control"] == "no-store"

    monkeypatch.setattr(updater, "apply_bundle_file",
                        lambda data: (False, "that file isn't a Family Office bundle"))
    r = test_client.post("/settings/upload-update",
                         files={"file": ("junk.zip", b"nope", "application/zip")})
    assert r.json() == {"ok": False, "message": "that file isn't a Family Office bundle"}

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

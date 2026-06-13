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

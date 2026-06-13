"""Tests for the in-app Help page (renders USAGE.md)."""


def test_help_page_renders_guide(test_client):
    r = test_client.get("/help")
    assert r.status_code == 200
    # Key sections are present (install + ingestion + usage).
    assert "User Guide" in r.text
    assert "Install" in r.text
    assert "Getting your data in" in r.text
    assert "CSV" in r.text and "JSON" in r.text and "XML" in r.text
    # Markdown was rendered to HTML, not shown raw.
    assert "<h1" in r.text and "<table" in r.text


def test_help_open_without_login(test_client):
    """Help stays reachable even when the access-code gate is on."""
    from app.config import settings

    settings.access_code = "secret"
    try:
        r = test_client.get("/help", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 200
    finally:
        settings.access_code = ""

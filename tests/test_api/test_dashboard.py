"""Tests for the dashboard and health API routes."""


def test_health_endpoint(test_client):
    """Health check returns 200."""
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_loads(test_client):
    """Dashboard page loads successfully."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert "Family Office" in response.text


def test_portfolio_page_loads(test_client):
    """Portfolio page loads."""
    response = test_client.get("/portfolio/")
    assert response.status_code == 200


def test_tax_summary_loads(test_client):
    """Tax summary page loads."""
    response = test_client.get("/tax/summary")
    assert response.status_code == 200


def test_approvals_page_loads(test_client):
    """Approvals page loads."""
    response = test_client.get("/approvals/")
    assert response.status_code == 200


def test_accounting_journal_loads(test_client):
    """Accounting journal page loads."""
    response = test_client.get("/accounting/journal")
    assert response.status_code == 200


def test_reports_page_loads(test_client):
    """Reports page loads."""
    response = test_client.get("/reports/")
    assert response.status_code == 200


def test_estate_ownership_loads(test_client):
    """Estate ownership page loads."""
    response = test_client.get("/estate/ownership")
    assert response.status_code == 200


def test_analysis_overview_loads(test_client):
    """Analysis overview page loads."""
    response = test_client.get("/analysis/")
    assert response.status_code == 200
    assert "Financial Analysis" in response.text


def test_analysis_risk_loads(test_client):
    """Risk analysis page loads."""
    response = test_client.get("/analysis/risk")
    assert response.status_code == 200


def test_analysis_profiles_loads(test_client):
    """Profiles page loads."""
    response = test_client.get("/analysis/profiles")
    assert response.status_code == 200


def test_analysis_monte_carlo_loads(test_client):
    """Monte Carlo page loads."""
    response = test_client.get("/analysis/monte-carlo")
    assert response.status_code == 200

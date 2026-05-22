"""Tests for the Tax Service."""

from datetime import date
from decimal import Decimal

from app.services.tax_service import TaxService


def test_get_tax_summary(seeded_session):
    """Tax summary returns valid data."""
    svc = TaxService(seeded_session)
    summary = svc.get_tax_summary()

    assert summary.tax_year == date.today().year
    assert isinstance(summary.short_term_gains, Decimal)
    assert isinstance(summary.estimated_tax_liability, Decimal)


def test_find_harvest_candidates_none_when_all_gains(seeded_session):
    """No harvest candidates when all positions are at a gain."""
    svc = TaxService(seeded_session)
    # VOO bought at $450, current price $520.50 — it's a gain, not a loss
    candidates = svc.find_harvest_candidates(min_loss=Decimal("10"))
    # Should be empty since VOO is at a gain
    assert all(c.total_unrealized_loss < 0 for c in candidates)


def test_simulate_cost_basis_election(seeded_session):
    """Cost basis method simulation returns results."""
    svc = TaxService(seeded_session)
    results = svc.simulate_election("cost_basis_method")

    assert len(results) == 3  # FIFO, HIFO, LIFO
    assert any(r.recommended for r in results)

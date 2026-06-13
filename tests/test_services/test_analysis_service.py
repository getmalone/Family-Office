"""Tests for the Analysis Service."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.analysis_service import AnalysisService


def test_get_risk_metrics_empty_portfolio(session):
    """Risk metrics returns zeros for empty portfolio."""
    svc = AnalysisService(session)
    metrics = svc.get_risk_metrics()

    assert metrics.portfolio_volatility == Decimal("0")
    assert metrics.sharpe_ratio == Decimal("0")
    assert metrics.max_drawdown == Decimal("0")
    assert metrics.lookback_days == 252


def test_get_risk_metrics_with_positions(seeded_session):
    """Risk metrics are computed for seeded portfolio."""
    svc = AnalysisService(seeded_session)
    metrics = svc.get_risk_metrics(lookback_days=252)

    # Should have non-zero values since we have positions
    assert metrics.lookback_days == 252
    assert len(metrics.position_risks) >= 1
    assert metrics.concentration.top_5_weight_pct > 0


def test_seed_default_profiles(session):
    """Seeding creates all system profiles, with the 5 risk levels included."""
    from app.services.analysis_service import DEFAULT_PROFILES

    svc = AnalysisService(session)
    svc.seed_default_profiles()

    profiles = svc.list_profiles()
    assert len(profiles) == len(DEFAULT_PROFILES)
    names = {p.name for p in profiles}
    assert {"Conservative", "Moderate", "Balanced", "Growth", "Aggressive"} <= names

    # Balanced should be active by default
    active = svc.get_active_profile()
    assert active is not None
    assert active.name == "Balanced"


def test_seed_default_profiles_idempotent(session):
    """Seeding profiles twice doesn't create duplicates."""
    from app.services.analysis_service import DEFAULT_PROFILES

    svc = AnalysisService(session)
    svc.seed_default_profiles()
    svc.seed_default_profiles()

    profiles = svc.list_profiles()
    assert len(profiles) == len(DEFAULT_PROFILES)


def test_set_active_profile(session):
    """Setting active profile deactivates others."""
    svc = AnalysisService(session)
    svc.seed_default_profiles()

    profiles = svc.list_profiles()
    conservative = next(p for p in profiles if p.name == "Conservative")
    aggressive = next(p for p in profiles if p.name == "Aggressive")

    result = svc.set_active_profile(aggressive.id)
    assert result.is_active is True
    assert result.name == "Aggressive"

    # Previous active should be deactivated
    active = svc.get_active_profile()
    assert active.name == "Aggressive"


def test_create_custom_profile(session):
    """Custom profile creation with valid allocations."""
    svc = AnalysisService(session)

    profile = svc.create_custom_profile(
        name="My Custom",
        target_allocations={
            "us_equity": Decimal("60"),
            "fixed_income": Decimal("30"),
            "cash": Decimal("10"),
        },
        description="Test custom profile",
    )

    assert profile.name == "My Custom"
    assert profile.risk_tolerance == "custom"
    assert profile.is_system is False
    assert profile.target_allocations["us_equity"] == Decimal("60")


def test_create_custom_profile_rejects_unbalanced(session):
    """Custom profile must sum to 100%."""
    svc = AnalysisService(session)

    with pytest.raises(ValueError, match="100%"):
        svc.create_custom_profile(
            name="Bad",
            target_allocations={"us_equity": Decimal("50")},
        )


def test_delete_custom_profile(session):
    """Custom profiles can be deleted, system profiles cannot."""
    svc = AnalysisService(session)
    svc.seed_default_profiles()

    custom = svc.create_custom_profile(
        name="Deletable",
        target_allocations={"us_equity": Decimal("100")},
    )
    assert svc.delete_profile(custom.id) is True
    assert len([p for p in svc.list_profiles() if p.name == "Deletable"]) == 0

    # System profile cannot be deleted
    balanced = next(p for p in svc.list_profiles() if p.name == "Balanced")
    assert svc.delete_profile(balanced.id) is False


def test_drift_analysis(seeded_session):
    """Drift analysis computes differences between current and target."""
    svc = AnalysisService(seeded_session)
    svc.seed_default_profiles()

    drift = svc.get_drift_analysis()
    assert drift.profile_name == "Balanced"
    assert len(drift.targets) > 0

    # Should have at least some overweight/underweight positions
    statuses = {t.drift_status for t in drift.targets}
    assert len(statuses) > 0  # At least one status type present


def test_drift_analysis_requires_profile(seeded_session):
    """Drift analysis raises if no active profile."""
    svc = AnalysisService(seeded_session)
    # No profiles seeded = no active profile

    with pytest.raises(ValueError, match="No active"):
        svc.get_drift_analysis()


def test_monte_carlo_basic(seeded_session):
    """Monte Carlo runs and returns valid percentile bands."""
    svc = AnalysisService(seeded_session)
    svc.seed_default_profiles()

    result = svc.run_monte_carlo(years=5, num_simulations=500)

    assert result.current.years == 5
    assert result.current.num_simulations == 500
    assert len(result.current.trajectories) == 5
    assert result.current.final_median > 0
    assert result.current.final_p10 <= result.current.final_median
    assert result.current.final_median <= result.current.final_p90

    # Should also have target result (Balanced is active)
    assert result.target is not None
    assert result.target.label.startswith("Target:")


def test_monte_carlo_with_goal(seeded_session):
    """Monte Carlo computes probability of reaching a goal."""
    svc = AnalysisService(seeded_session)
    svc.seed_default_profiles()

    result = svc.run_monte_carlo(
        years=10,
        num_simulations=1000,
        goal_amount=Decimal("2000000"),
    )

    assert result.current.probability_of_goal is not None
    assert Decimal("0") <= result.current.probability_of_goal <= Decimal("100")
    assert result.current.goal_amount == Decimal("2000000")
    assert result.goal_amount == Decimal("2000000")


def test_monte_carlo_percentile_ordering(seeded_session):
    """Percentiles should be ordered: p10 <= p25 <= median <= p75 <= p90."""
    svc = AnalysisService(seeded_session)
    svc.seed_default_profiles()

    result = svc.run_monte_carlo(years=10, num_simulations=1000)

    for t in result.current.trajectories:
        assert t.p10 <= t.p25
        assert t.p25 <= t.median
        assert t.median <= t.p75
        assert t.p75 <= t.p90

"""Tests for the max-sustainable-spending solver."""

from app.services.analysis_service import AnalysisService


def test_solver_meets_target_and_is_monotonic(seeded_session):
    svc = AnalysisService(seeded_session)
    common = dict(years=0, num_simulations=800, withdrawal_years=30,
                  regime_override="base")  # base = skip Markov (no network in tests)

    r85 = svc.solve_max_spending(target_survival=85.0, **common)
    r95 = svc.solve_max_spending(target_survival=95.0, **common)

    # Highest rate that still meets the target → achieved survival ≥ target.
    assert float(r85.achieved_survival) >= 85.0
    assert float(r95.achieved_survival) >= 95.0

    # A stricter survival target cannot allow more spending.
    assert r95.max_withdrawal_rate <= r85.max_withdrawal_rate

    # Sane outputs.
    assert r85.annual_amount >= 0
    assert float(r85.monthly_amount) == round(float(r85.annual_amount) / 12.0, 2)
    assert r85.target_survival == 85


def test_solver_with_social_security(seeded_session):
    """SSA income should allow at least as much spending as without it."""
    svc = AnalysisService(seeded_session)
    base = dict(num_simulations=800, withdrawal_years=30, regime_override="base",
                current_age=60, retirement_age=60, target_survival=85.0)

    without = svc.solve_max_spending(**base)
    withssa = svc.solve_max_spending(ssa_claiming_age=67, ssa_monthly_fra=__import__("decimal").Decimal("3000"), **base)

    assert withssa.max_withdrawal_rate >= without.max_withdrawal_rate

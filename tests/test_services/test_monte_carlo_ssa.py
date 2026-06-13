"""Tests for Social Security modeling in the Monte Carlo distribution phase."""

import pytest

from app.services.analysis_service import AnalysisService


# ── Claiming-age benefit factor (FRA = 67) ─────────────────────────────────

@pytest.mark.parametrize("age,expected", [
    (62, 0.70),     # 5 years early → 70%
    (63, 0.75),
    (64, 0.80),
    (65, 0.8667),
    (66, 0.9333),
    (67, 1.00),     # full retirement age
    (68, 1.08),     # +8%/yr delayed credit
    (70, 1.24),     # max delayed credit
])
def test_ssa_claiming_factor(age, expected):
    assert round(AnalysisService._ssa_claiming_factor(age), 4) == expected


def test_ssa_claiming_factor_clamped():
    # Below 62 clamps to 62 (70%); above 70 clamps to 70 (124%).
    assert AnalysisService._ssa_claiming_factor(50) == AnalysisService._ssa_claiming_factor(62)
    assert AnalysisService._ssa_claiming_factor(99) == AnalysisService._ssa_claiming_factor(70)


# ── SSA offset inside the simulation ───────────────────────────────────────

def _sim(session, **kw):
    """Run a single deterministic simulation (RNG is seeded)."""
    svc = AnalysisService(session)
    defaults = dict(
        label="t",
        allocation={"us_equity": 0.6, "fixed_income": 0.4},
        initial_value=1_000_000.0,
        years=0,                 # retire immediately → start value is deterministic
        num_simulations=3000,
        goal_amount=None,
        withdrawal_rate=8.0,     # aggressive, so ruin is meaningful without SSA
        withdrawal_years=30,
    )
    defaults.update(kw)
    return svc._simulate(**defaults)


def test_ssa_improves_survival(session):
    """Adding SSA income reduces the portfolio draw and raises the survival rate."""
    without = _sim(session)
    with_ssa = _sim(
        session,
        current_age=60, retirement_age=60, ssa_claiming_age=67,
        ssa_annual_benefit=40_000.0,
    )
    assert without.ssa_annual_benefit is None
    assert with_ssa.portfolio_survival_rate > without.portfolio_survival_rate


def test_ssa_fields_populated(session):
    r = _sim(
        session,
        current_age=60, retirement_age=60, ssa_claiming_age=67,
        ssa_annual_benefit=40_000.0, ssa_fra_factor=1.0,
    )
    assert r.ssa_claiming_age == 67
    assert float(r.ssa_annual_benefit) == 40_000.0
    assert float(r.ssa_monthly_benefit) == pytest.approx(40_000 / 12, abs=0.01)
    # Retire at 60, claim at 67 → 7 bridge years funded by the portfolio alone.
    assert r.bridge_years == 7
    # SSA flows for the remaining 23 of 30 years.
    assert float(r.ssa_total_benefit) == pytest.approx(40_000.0 * 23, abs=1.0)
    # Net portfolio draw once SSA starts = Go-Go spend − SSA.
    expected_net = max(float(r.withdrawal_annual_amount) - 40_000.0, 0.0)
    assert float(r.net_draw_after_ssa) == pytest.approx(expected_net, abs=1.0)


def test_bridge_years_track_claiming_age(session):
    early = _sim(session, current_age=60, retirement_age=60,
                 ssa_claiming_age=62, ssa_annual_benefit=30_000.0)
    late = _sim(session, current_age=60, retirement_age=60,
                ssa_claiming_age=70, ssa_annual_benefit=30_000.0)
    assert early.bridge_years == 2
    assert late.bridge_years == 10


def test_no_ssa_is_backward_compatible(session):
    """Without ages/benefit, all SSA fields stay None and the run is unchanged."""
    a = _sim(session)
    b = _sim(session)  # same seed → identical
    assert a.ssa_annual_benefit is None
    assert a.bridge_years is None
    assert a.ssa_claiming_age is None
    assert a.portfolio_survival_rate == b.portfolio_survival_rate


def test_ssa_only_after_claiming_age_disabled_without_both_ages(session):
    """A benefit with no retirement/claiming age can't be placed in time → ignored."""
    r = _sim(session, ssa_annual_benefit=40_000.0)  # no ages
    assert r.ssa_annual_benefit is None
    assert r.net_draw_after_ssa is None

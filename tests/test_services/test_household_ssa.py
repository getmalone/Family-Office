"""Tests for the two-earner (married-couple) Social Security model.

Covers the spousal-reduction factor, two streams on each spouse's own age
timeline, the spousal top-up (and its both-must-claim gate), the survivor
benefit, single-person backward compatibility, and the income bridge netting
both streams.
"""

from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.services.analysis_service import AnalysisService


def _svc_no_db():
    return AnalysisService.__new__(AnalysisService)


# ── Spousal reduction factor ───────────────────────────────────────────────

def test_spousal_factor():
    f = AnalysisService._ssa_spousal_factor
    assert f(67) == 1.0                         # at FRA → full 50%
    assert f(70) == 1.0                         # capped at FRA — no delayed credit on spousal
    assert round(f(62), 2) == 0.65              # ≈32.5% of PIA (0.5 × 0.65)
    assert f(64) > f(62)                        # less reduction the later you claim


# ── Two streams on independent age timelines ───────────────────────────────

def test_two_streams_separate_timelines():
    svc = _svc_no_db()
    # Primary retires 60 (accum 8 → age 52 today), claims 67. Spouse is 50 today,
    # claims 62 → reaches 62 four years into the 8-year accumulation, so their
    # benefit is counted from the first distribution year onward.
    sched, hh = svc._household_ssa(
        withdrawal_years=30, accumulation_years=8, retirement_age=60,
        primary_monthly_fra=3000, primary_claiming_age=67,
        spouse_monthly_fra=1500, spouse_claiming_age=62, spouse_current_age=50,
    )
    you, spouse = hh.people
    assert you.label == "You" and you.starts_plan_year == 8       # age 67 at plan year 8
    assert spouse.label == "Spouse" and spouse.starts_plan_year == 5
    # First SSA dollar = the spouse's, four bridge years before it.
    assert hh.first_benefit_plan_year == 5
    assert sched[0] == 0.0 and sched[4] > 0.0                     # nothing yr1, spouse from yr5


def test_spousal_topup_applies_to_low_earner():
    svc = _svc_no_db()
    # Lower earner's own benefit (500) is below 50% of the higher FRA (1500),
    # so they're topped up to the spousal amount.
    _, hh = svc._household_ssa(
        withdrawal_years=20, accumulation_years=0, retirement_age=67,
        primary_monthly_fra=3000, primary_claiming_age=67,
        spouse_monthly_fra=500, spouse_claiming_age=67, spouse_current_age=67,
    )
    spouse = next(p for p in hh.people if p.label == "Spouse")
    assert spouse.own_monthly_benefit == Decimal("500.00")
    assert spouse.spousal_monthly_benefit == Decimal("1000.00")  # 1500 spousal − 500 own
    assert spouse.monthly_benefit == Decimal("1500.00")          # topped up to 50% of higher FRA


def test_spousal_requires_both_claimed():
    svc = _svc_no_db()
    # Higher earner delays to 70; lower earner (spouse) claims at 62. The spousal
    # top-up is NOT payable until the higher earner has also filed.
    sched, hh = svc._household_ssa(
        withdrawal_years=20, accumulation_years=0, retirement_age=62,
        primary_monthly_fra=3000, primary_claiming_age=70,
        spouse_monthly_fra=800, spouse_claiming_age=62, spouse_current_age=62,
    )
    spouse = next(p for p in hh.people if p.label == "Spouse")
    own_only_annual = float(spouse.own_monthly_benefit) * 12       # 800 × 0.70 × 12
    # Year 1: only the spouse's own benefit (primary hasn't claimed → no top-up).
    assert round(sched[0]) == round(own_only_annual)
    # Year 9 (primary now 70): both stream + the spousal top-up are flowing.
    assert sched[8] > sched[0] + float(spouse.spousal_monthly_benefit) * 6


def test_survivor_keeps_larger_benefit():
    svc = _svc_no_db()
    _, hh = svc._household_ssa(
        withdrawal_years=20, accumulation_years=0, retirement_age=67,
        primary_monthly_fra=3000, primary_claiming_age=67,
        spouse_monthly_fra=1400, spouse_claiming_age=62, spouse_current_age=67,
    )
    # Survivor keeps the larger of the two: the primary's $3,000/mo → $36,000/yr.
    assert hh.survivor_annual_benefit == Decimal("36000.00")


def test_single_person_unchanged():
    svc = _svc_no_db()
    sched, hh = svc._household_ssa(
        withdrawal_years=15, accumulation_years=8, retirement_age=60,
        primary_monthly_fra=3000, primary_claiming_age=67,
    )
    assert hh is None                                            # no couple summary
    assert sched[6] == 0.0 and sched[7] == 36000.0              # starts age 67 (plan year 8)


# ── Integration: couple SSA + income bridge through run_monte_carlo ─────────

def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_run_monte_carlo_couple_and_bridge():
    svc = AnalysisService(_session())
    cmp = svc.run_monte_carlo(
        num_simulations=800, withdrawal_rate=Decimal("4.5"), withdrawal_years=30,
        current_age=52, retirement_age=60,
        ssa_claiming_age=67, ssa_monthly_fra=Decimal("3200"),
        spouse_current_age=50, spouse_claiming_age=62, spouse_monthly_fra=Decimal("1400"),
        income_bridge=True,
        bucket_balances={"taxable": 700_000, "traditional": 900_000, "roth": 400_000},
    )
    r = cmp.current
    assert r.ssa_household is not None
    assert len(r.ssa_household.people) == 2
    assert r.ssa_household.combined_annual_benefit > 0
    # The spouse claims earlier (62) than the primary (67), so the bridge is the
    # years before the spouse's benefit — both the SSA summary and the income
    # bridge agree on it.
    assert r.bridge_years == r.income_bridge.bridge_years
    assert 0 < r.bridge_years < 7
    # Earlier-claiming spouse shortens the bridge vs. the primary alone (claim 67
    # with retirement at 60 would be a 7-year bridge).
    assert r.bridge_years < 7

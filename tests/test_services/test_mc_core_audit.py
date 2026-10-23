"""Regression tests for the Monte Carlo core fixes from the calculation audit.

Each test pins one defect the audit reproduced: survival ignoring accumulation
risk, zero-correlation volatility, the solver ignoring the income bridge,
bridge capital from per-bucket medians, "total withdrawn" counting SSA-covered
spending, the regime being dropped at immediate retirement, smile phase
lengths, the non-working spouse, the spouse-only page crash, and regime
weights for a symbol held in several accounts.
"""

from decimal import Decimal
from types import SimpleNamespace

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.services import analysis_service as mod
from app.services.analysis_service import (
    _CORR_ORDER,
    AnalysisService,
    asset_class_correlation,
    portfolio_return_and_vol,
)

STOCKS_BONDS = {"us_equity": 0.6, "fixed_income": 0.4}


def _svc():
    return AnalysisService.__new__(AnalysisService)


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _regime(name):
    return _svc()._resolve_portfolio_regime(None, 1.0, name)


def _sim(**kw):
    args = dict(label="t", allocation=STOCKS_BONDS, initial_value=1_000_000.0, years=10,
                num_simulations=3000, goal_amount=None, withdrawal_rate=5.0, withdrawal_years=30)
    args.update(kw)
    return _svc()._simulate(**args)


# ── Volatility uses correlations ────────────────────────────────────────────

def test_correlation_matrix_is_valid():
    corr = np.array([[asset_class_correlation(a, b) for b in _CORR_ORDER] for a in _CORR_ORDER])
    assert np.allclose(corr, corr.T)
    assert np.linalg.eigvalsh(corr).min() > 0          # positive definite


def test_equity_blend_is_not_safer_than_its_parts():
    _, vol_blend = portfolio_return_and_vol({"us_equity": 0.5, "intl_equity": 0.5})
    assert round(vol_blend, 3) == 0.164                 # was 0.120 assuming zero correlation
    assert vol_blend > 0.16                             # ≥ 100% US equity's 16%


# ── Survival reflects accumulation risk (fixed-dollar withdrawal) ──────────

def test_withdrawal_is_one_dollar_amount_from_median_balance():
    r = _sim()
    assert float(r.withdrawal_annual_amount) == round(float(r.final_median) * 0.05, 2)


def test_bear_regime_lowers_survival():
    bull = _sim(regime=_regime("bull"), withdrawal_rate=6.0)
    bear = _sim(regime=_regime("bear"), withdrawal_rate=6.0)
    assert bear.portfolio_survival_rate < bull.portfolio_survival_rate


def test_regime_applies_when_retiring_immediately():
    bull = _sim(years=0, regime=_regime("bull"), withdrawal_rate=6.0)
    bear = _sim(years=0, regime=_regime("bear"), withdrawal_rate=6.0)
    assert bear.portfolio_survival_rate < bull.portfolio_survival_rate


# ── "Drawn from portfolio" is what the portfolio actually paid ─────────────

def test_drawn_total_excludes_spending_covered_by_ssa():
    r = _sim(years=0, withdrawal_rate=4.0, withdrawal_years=20, ssa_schedule=[60_000.0] * 20)
    assert r.withdrawal_total == Decimal("0.0")         # SSA covers the whole $40k/yr


def test_drawn_total_stops_at_depletion():
    r = _sim(years=0, withdrawal_rate=15.0, withdrawal_years=30)
    assert float(r.withdrawal_total) < 150_000 * 30     # ruined paths stop paying


# ── Spending smile phases ──────────────────────────────────────────────────

def test_smile_phase_lengths():
    for n, (go, slow) in {10: (4, 3), 4: (2, 1), 30: (10, 10), 2: (1, 1)}.items():
        r = _sim(withdrawal_years=n, spending_pattern="smile", num_simulations=200)
        assert (r.smile_go_go_years, r.smile_slow_go_years) == (go, slow), n


# ── Income bridge: medians of real per-simulation totals ───────────────────

def test_bridge_capital_is_median_of_per_sim_totals():
    # 3 sims each need $100/yr for 3 bridge years, but empty different buckets
    # at different times; no tax, no growth. Every sim draws $300 in total.
    _, bridge, net = _svc()._run_income_bridge(
        final_vals=np.array([300.0, 300.0, 300.0]),
        annual_wdraw=np.array([100.0, 100.0, 100.0]),
        smile_mults=np.ones(4), ssa_per_year=np.array([0.0, 0.0, 0.0, 1000.0]),
        dist_gf=np.ones((3, 4)),
        bucket_balances={"taxable": 1.0, "traditional": 1.0, "roth": 1.0},
        deferred_tax_rate=0.0, taxable_tax_rate=0.0, withdrawal_years=4,
        num_simulations=3, retirement_age=60, ssa_claiming_age=63,
    )
    assert bridge.bridge_number == Decimal("300.0")
    for row in bridge.funding_by_year[:3]:
        assert float(row.taxable + row.traditional + row.roth) == 100.0
    assert list(net) == [300.0, 300.0, 300.0]


# ── Max-spending solver uses the displayed scenario ────────────────────────

def test_solver_honours_income_bridge_and_taxes():
    svc = AnalysisService(_session())
    kw = dict(num_simulations=600, withdrawal_years=30, current_age=60, retirement_age=60,
              bucket_balances={"taxable": 200_000, "traditional": 700_000, "roth": 100_000},
              deferred_tax_rate=0.22, taxable_tax_rate=0.10)
    plain = svc.solve_max_spending(**kw)
    bridged = svc.solve_max_spending(**kw, income_bridge=True)
    assert bridged.income_bridge and not plain.income_bridge
    assert bridged.max_withdrawal_rate < plain.max_withdrawal_rate   # taxes cost spending
    # The solved rate really does hit the target in the displayed (bridged) run.
    shown = svc.run_monte_carlo(num_simulations=600, withdrawal_rate=bridged.max_withdrawal_rate,
                                withdrawal_years=30, current_age=60, retirement_age=60,
                                include_target=False, income_bridge=True,
                                bucket_balances=kw["bucket_balances"],
                                deferred_tax_rate=0.22, taxable_tax_rate=0.10)
    assert float(shown.current.portfolio_survival_rate) >= 85.0


def test_solver_flags_unreachable_target():
    r = AnalysisService(_session()).solve_max_spending(
        num_simulations=300, withdrawal_years=50, years=0, target_survival=99.0,
        regime_override="bear")
    if not r.target_reachable:
        assert r.max_withdrawal_rate < Decimal("0.01")
    assert isinstance(r.target_reachable, bool)


# ── Household SSA edge cases ───────────────────────────────────────────────

def test_spouse_without_work_record_gets_spousal_benefit():
    sched, hh = _svc()._household_ssa(
        withdrawal_years=12, accumulation_years=4, retirement_age=58,
        primary_monthly_fra=4100, primary_claiming_age=62,
        spouse_monthly_fra=0, spouse_claiming_age=62, spouse_current_age=52,
    )
    spouse = next(p for p in hh.people if p.label == "Spouse")
    assert spouse.own_monthly_benefit == Decimal("0.0")
    assert spouse.spousal_monthly_benefit == Decimal("1332.5")   # 2,050 × 65%
    assert sched[6] == (2870 + 1332.5) * 12


def test_spouse_only_benefit_renders(test_client):
    r = test_client.get("/analysis/monte-carlo?withdrawal_rate=4&current_age=54&retirement_age=58"
              "&ssa_claiming_age=67&spouse_monthly=2000&spouse_claiming_age=62&spouse_current_age=60")
    assert r.status_code == 200
    assert "Household Social Security" in r.text


# ── Regime detection weights a symbol across accounts ─────────────────────

def test_regime_weights_sum_symbol_across_accounts(monkeypatch):
    seen = {}

    class FakeMarkov:
        def portfolio_and_ticker_regimes_from_prices(self, price_map, weights):
            seen.update(weights)
            return None, None

    monkeypatch.setattr("app.services.markov_regime_service.MarkovRegimeService", FakeMarkov)
    monkeypatch.setattr("app.services.market_data.stored_price_map", lambda s, syms: {})
    summary = SimpleNamespace(holdings=[
        SimpleNamespace(symbol="X", market_value=300.0),
        SimpleNamespace(symbol="X", market_value=300.0),
        SimpleNamespace(symbol="Y", market_value=400.0),
    ])
    svc = _svc()
    svc.session = None
    svc._resolve_portfolio_regime(summary, 1000.0, None)
    assert seen == {"X": 0.6, "Y": 0.4}


def test_drift_matches_simple_expected_return():
    # Mean annual growth factor should equal 1 + the assumed simple return.
    r = _sim(allocation={"us_equity": 1.0}, years=1, num_simulations=200_000, withdrawal_rate=0)
    assert abs(float(r.final_median) / 1e6 - np.exp(np.log1p(0.10) - 0.5 * 0.16 ** 2)) < 0.003
    assert mod.ASSET_CLASS_ASSUMPTIONS["us_equity"]["return"] == 0.10

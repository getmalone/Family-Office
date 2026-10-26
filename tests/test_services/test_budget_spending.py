"""Budget-driven spending (itemised fixed + variable costs), inflation with
results in today's dollars, the budget max-spending solver, and the expense
baseline pre-fill."""

from datetime import date, timedelta
from decimal import Decimal

from app.api.analysis import _apply_buffer, _parse_budget
from app.services.analysis_service import AnalysisService, budget_totals

ALLOC = {"us_equity": 0.6, "fixed_income": 0.4}


def _svc():
    return AnalysisService.__new__(AnalysisService)


def _sim(**kw):
    args = dict(label="t", allocation=ALLOC, initial_value=1_500_000.0, years=4, num_simulations=2000,
                goal_amount=None, withdrawal_years=30)
    args.update(kw)
    return _svc()._simulate(**args)


def test_budget_totals_and_parsing():
    items = _parse_budget('[{"name":"Taxes","monthly":"533","fixed":true},'
                          '{"name":"Travel","monthly":1000,"fixed":false},{"bad":1},"x"]')
    assert items[0] == {"name": "Taxes", "monthly": 533.0, "fixed": True}
    assert budget_totals(items) == (533 * 12, 1000 * 12)
    assert _apply_buffer(items, 10)[0]["monthly"] == 586.3        # +10% buffer


def test_budget_mode_spends_the_budget():
    r = _sim(spending_fixed_annual=44_000, spending_variable_annual=0)
    assert r.spending_mode == "budget"
    assert float(r.withdrawal_annual_amount) == 44_000
    assert r.implied_withdrawal_rate == Decimal(str(round(44_000 / float(r.final_median) * 100, 2)))


def test_smile_only_reduces_variable_costs():
    all_fixed = _sim(spending_fixed_annual=60_000, spending_variable_annual=0, spending_pattern="smile",
                     smile_slow_pct=50, smile_no_pct=50)
    no_smile = _sim(spending_fixed_annual=60_000, spending_variable_annual=0)
    assert all_fixed.portfolio_survival_rate == no_smile.portfolio_survival_rate   # nothing to scale down
    mixed = _sim(spending_fixed_annual=30_000, spending_variable_annual=30_000, spending_pattern="smile",
                 smile_slow_pct=50, smile_no_pct=50)
    assert float(mixed.withdrawal_total) < float(no_smile.withdrawal_total)


def test_bigger_budget_lowers_survival():
    lo = _sim(spending_fixed_annual=60_000, spending_variable_annual=0)
    hi = _sim(spending_fixed_annual=120_000, spending_variable_annual=0)
    assert hi.portfolio_survival_rate < lo.portfolio_survival_rate


def test_inflation_grows_spending_and_reports_todays_dollars():
    nominal = _sim(withdrawal_rate=4.5)
    real = _sim(withdrawal_rate=4.5, inflation_rate=0.025)
    assert real.portfolio_survival_rate < nominal.portfolio_survival_rate       # spending keeps rising
    # Same draws; the retirement balance is just restated in today's dollars.
    assert abs(float(real.final_median) - float(nominal.final_median) / 1.025 ** 4) < 1.0
    assert real.inflation_rate == Decimal("2.5")


def test_ssa_gets_the_same_cola():
    # Social Security covering the whole budget keeps covering it with inflation.
    r = _sim(spending_fixed_annual=40_000, spending_variable_annual=0, inflation_rate=0.03,
             ssa_schedule=[45_000.0] * 30)
    assert r.withdrawal_total == Decimal("0.0") and r.portfolio_survival_rate == Decimal("100.0")


def test_zero_inflation_matches_original_model():
    a = _sim(withdrawal_rate=5.0)
    b = _sim(withdrawal_rate=5.0, inflation_rate=0.0)
    assert (a.portfolio_survival_rate, a.final_median, a.withdrawal_total) == \
           (b.portfolio_survival_rate, b.final_median, b.withdrawal_total)


def test_solver_in_budget_mode(session):
    svc = AnalysisService(session)
    budget = [{"name": "Everything", "monthly": 3670, "fixed": True}]
    kw = dict(num_simulations=400, withdrawal_years=30, current_age=60, retirement_age=60,
              income_bridge=True, bucket_balances={"taxable": 400_000, "traditional": 400_000, "roth": 200_000},
              inflation_rate=0.025)
    r = svc.solve_max_spending(**kw, budget_items=budget)
    assert r.spending_mode == "budget" and r.budget_monthly == Decimal("3670.0")
    # $1M over 30 years after taxes and inflation can't carry $3,670/mo: >100% of max.
    assert abs(float(r.budget_pct_of_max) - 3670 / float(r.monthly_amount) * 100) < 0.2
    # The solved budget really meets the target in a normal run.
    scale = float(r.monthly_amount) / 3670
    shown = svc.run_monte_carlo(num_simulations=400, withdrawal_years=30, current_age=60, retirement_age=60,
                                include_target=False, income_bridge=True,
                                bucket_balances=kw["bucket_balances"], inflation_rate=0.025,
                                budget_items=[{"name": "Everything", "monthly": 3670 * scale, "fixed": True}])
    assert float(shown.current.portfolio_survival_rate) >= 84.5


def test_expense_baseline(session):
    from app.models.expense import Expense, ExpenseCategory
    taxes = ExpenseCategory(name="Property Taxes")
    travel = ExpenseCategory(name="Travel")
    session.add_all([taxes, travel])
    session.flush()
    for k in range(12):
        d = date.today() - timedelta(days=30 * k + 1)
        session.add(Expense(category_id=taxes.id, amount=Decimal("533"), expense_date=d, description="tax"))
        session.add(Expense(category_id=travel.id, amount=Decimal("100"), expense_date=d, description="trip"))
    session.add(Expense(category_id=travel.id, amount=Decimal("9999"), expense_date=date.today() - timedelta(days=500),
                        description="too old"))
    session.flush()
    items = {i["name"]: i for i in AnalysisService(session).expense_baseline()}
    assert items["Property Taxes"] == {"name": "Property Taxes", "monthly": 533.0, "fixed": True}
    assert items["Travel"]["monthly"] == 100.0 and items["Travel"]["fixed"] is False


def test_page_runs_budget_mode_and_remembers_it(test_client):
    import json
    budget = [{"name": "Property Taxes", "monthly": 533, "fixed": True},
              {"name": "Food & Groceries", "monthly": 800, "fixed": True}]
    r = test_client.get("/analysis/monte-carlo", params={
        "spending_mode": "budget", "budget_items": json.dumps(budget), "budget_buffer": 10,
        "inflation_rate": 2.5, "withdrawal_years": 30, "current_age": 54, "retirement_age": 58,
        "simulations": 500})
    assert r.status_code == 200
    assert "budget ≈" in r.text and "today's dollars" in r.text
    again = test_client.get("/analysis/monte-carlo", params={"spending_mode": "budget", "simulations": 500})
    assert "Food &amp; Groceries" in again.text or "Food & Groceries" in again.text   # saved budget reloaded


def test_surplus_ignored_unless_reinvested():
    # SSA of $60k against a $40k budget leaves $20k/yr extra from year 1.
    base = dict(spending_fixed_annual=40_000, spending_variable_annual=0, ssa_schedule=[60_000.0] * 30)
    ignored = _sim(**base)
    reinvested = _sim(**base, reinvest_surplus=True)
    assert ignored.withdrawal_total == reinvested.withdrawal_total == Decimal("0.0")   # never spent
    assert float(reinvested.withdrawal_final_median) > float(ignored.withdrawal_final_median) + 20_000 * 25


def test_surplus_reinvested_under_income_bridge():
    base = dict(spending_fixed_annual=40_000, spending_variable_annual=0, ssa_schedule=[60_000.0] * 30,
                income_bridge=True, bucket_balances={"taxable": 1, "traditional": 1, "roth": 1})
    assert float(_sim(**base, reinvest_surplus=True).withdrawal_final_median) > \
           float(_sim(**base).withdrawal_final_median)


def test_page_remembers_mode_and_surplus_choice(test_client):
    import json
    budget = [{"name": "Property Taxes", "monthly": 533, "fixed": True},
              {"name": "Variable / discretionary", "monthly": 1000, "fixed": False}]
    test_client.get("/analysis/monte-carlo", params={
        "spending_mode": "budget", "budget_items": json.dumps(budget), "reinvest_surplus": "true",
        "simulations": 500})
    r = test_client.get("/analysis/monte-carlo", params={"simulations": 500})   # plain revisit
    assert 'value="budget" form="mc-form"\n                 checked' in r.text or \
           'value="budget" form="mc-form"\n                 checked' in r.text.replace("\r", "")
    assert 'name="reinvest_surplus" value="true" form="mc-form" class="mt-0.5" checked' in r.text
    assert 'id="budget-variable" min="0" step="50" value="1000"' in r.text
    # Submitting in rate mode without the box clears the surplus choice.
    test_client.get("/analysis/monte-carlo", params={"spending_mode": "rate", "simulations": 500})
    r = test_client.get("/analysis/monte-carlo", params={"simulations": 500})
    assert 'class="mt-0.5" checked' not in r.text

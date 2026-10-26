"""Income bridge taxes only the GAIN part of taxable withdrawals: the cost
basis comes back tax-free (average-cost selling)."""

from datetime import date
from decimal import Decimal

import numpy as np

from app.services.analysis_service import AnalysisService


def _bridge(taxable_basis, years=3, need=103_396.0, value=2_000_000.0):
    # Three pre-SSA years of $103,396 from a taxable-only bucket, no growth, 10% rate.
    return AnalysisService.__new__(AnalysisService)._run_income_bridge(
        final_vals=np.array([value]), spending_by_year=np.full(years + 1, need),
        ssa_per_year=np.array([0.0] * years + [need]), dist_gf=np.ones((1, years + 1)),
        bucket_balances={"taxable": 1.0, "traditional": 0.0, "roth": 0.0},
        deferred_tax_rate=0.18, taxable_tax_rate=0.10, withdrawal_years=years + 1,
        num_simulations=1, retirement_age=58, ssa_claiming_age=61, taxable_basis=taxable_basis)[1]


def test_unknown_basis_keeps_the_old_full_gross_up():
    b = _bridge(None)
    assert abs(float(b.bridge_number) - 103_396 * 3 / 0.9) < 1      # $344,653
    assert b.taxable_gain_share_pct is None


def test_only_gains_are_taxed():
    half = _bridge(1_000_000.0)                                       # 50% of the account is gain
    assert float(half.taxable_gain_share_pct) == 50.0
    # ≈ 310,188 / (1 − 0.10 × 0.5) = 326,513 — between no-tax and the old full gross-up.
    assert 310_188 < float(half.bridge_number) < 327_000
    no_gain = _bridge(2_000_000.0)                                    # all basis → no tax
    assert abs(float(no_gain.bridge_number) - 103_396 * 3) < 1
    assert float(no_gain.taxes_total_median) == 0.0


def test_basis_runs_down_as_shares_are_sold():
    # Average-cost selling keeps the gain share constant without growth; with
    # growth, the gain share (and tax) rises over time.
    flat = AnalysisService.__new__(AnalysisService)._run_income_bridge(
        final_vals=np.array([1_000_000.0]), spending_by_year=np.full(5, 100_000.0),
        ssa_per_year=np.zeros(5), dist_gf=np.full((1, 5), 1.10),
        bucket_balances={"taxable": 1.0, "traditional": 0.0, "roth": 0.0},
        deferred_tax_rate=0.0, taxable_tax_rate=0.20, withdrawal_years=5, num_simulations=1,
        retirement_age=60, ssa_claiming_age=None, taxable_basis=1_000_000.0)[1]
    gross = [float(r.taxable) for r in flat.funding_by_year]
    assert gross == sorted(gross) and gross[-1] > gross[0]            # later sales carry more gain


def test_basis_from_tax_lots(session):
    from app.models.account import Account, AccountTypeEnum
    from app.models.asset import Asset, AssetClassEnum, AssetPrice
    from app.services.portfolio_service import PortfolioService
    acct = Account(name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, institution="T", is_taxable=True)
    ira = Account(name="IRA", account_type=AccountTypeEnum.IRA_TRADITIONAL, institution="T", is_taxable=False)
    a = Asset(symbol="VOO", name="VOO", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add_all([acct, ira, a])
    session.flush()
    session.add(AssetPrice(asset_id=a.id, price_date=date.today(), close_price=Decimal("500"), source="t"))
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, Decimal(1000), Decimal(250), transaction_date=date(2020, 1, 2))
    ps.execute_buy(ira.id, a.id, Decimal(1000), Decimal(100), transaction_date=date(2020, 1, 2))
    assert ps.get_taxable_cost_basis() == Decimal(250_000)           # IRA basis excluded
    r = AnalysisService(session).run_monte_carlo(
        num_simulations=200, withdrawal_rate=Decimal("4"), withdrawal_years=20, years=0,
        include_target=False, income_bridge=True)
    assert abs(float(r.current.income_bridge.taxable_gain_share_pct) - 50.0) < 0.1   # $500k value, $250k basis

"""Tests for the Monte Carlo Income Bridge: account-type withdrawal sequencing.

Covers the deferred half of the Social Security question — *which accounts fund
the pre-SSA years*: bucketing real accounts by tax treatment, the ordered
taxable → traditional → Roth drawdown, the effective-tax gross-up, and the
Gap/Bridge numbers.
"""

from datetime import date
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum, tax_bucket_for
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.analysis_service import AnalysisService
from app.services.portfolio_service import PortfolioService


# ── Account → tax bucket mapping ───────────────────────────────────────────

def test_tax_bucket_mapping():
    assert tax_bucket_for(AccountTypeEnum.BROKERAGE) == "taxable"
    assert tax_bucket_for(AccountTypeEnum.TRUST) == "taxable"
    assert tax_bucket_for(AccountTypeEnum.BANK_SAVINGS) == "taxable"
    assert tax_bucket_for(AccountTypeEnum.IRA_TRADITIONAL) == "traditional"
    assert tax_bucket_for(AccountTypeEnum.FOUR01K) == "traditional"
    assert tax_bucket_for(AccountTypeEnum.IRA_ROTH) == "roth"
    assert tax_bucket_for(AccountTypeEnum.HSA) == "roth"
    # Accepts the raw string value and falls back to taxable on the unknown.
    assert tax_bucket_for("401k") == "traditional"
    assert tax_bucket_for("not_a_real_type") == "taxable"


# ── Bucketing real account balances ────────────────────────────────────────

def _add_position(session, account, asset, qty, price):
    txn = Transaction(
        account_id=account.id, asset_id=asset.id,
        transaction_type=TransactionTypeEnum.BUY, transaction_date=date(2023, 1, 2),
        quantity=Decimal(qty), price_per_unit=Decimal(price),
        total_amount=Decimal(qty) * Decimal(price), fees=Decimal("0"),
    )
    session.add(txn)
    session.flush()
    session.add(TaxLot(
        account_id=account.id, asset_id=asset.id,
        acquisition_date=date(2023, 1, 2), acquisition_transaction_id=txn.id,
        original_quantity=Decimal(qty), remaining_quantity=Decimal(qty),
        cost_basis_per_unit=Decimal(price), original_cost_basis_per_unit=Decimal(price),
    ))


def test_get_tax_bucket_balances(session):
    asset = Asset(symbol="VOO", name="Vanguard S&P 500",
                  asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(asset)
    session.flush()
    session.add(AssetPrice(asset_id=asset.id, price_date=date.today(),
                           close_price=Decimal("100.00"), source="test"))

    accts = {
        "taxable": Account(name="Brokerage", account_type=AccountTypeEnum.BROKERAGE, is_taxable=True),
        "traditional": Account(name="Rollover IRA", account_type=AccountTypeEnum.IRA_TRADITIONAL, is_taxable=False),
        "roth": Account(name="Roth IRA", account_type=AccountTypeEnum.IRA_ROTH, is_taxable=False),
    }
    session.add_all(accts.values())
    session.flush()
    _add_position(session, accts["taxable"], asset, "100", "100.00")      # $10,000
    _add_position(session, accts["traditional"], asset, "50", "100.00")   # $5,000
    _add_position(session, accts["roth"], asset, "30", "100.00")          # $3,000
    session.flush()

    balances = PortfolioService(session).get_tax_bucket_balances()
    assert balances["taxable"] == Decimal("10000.00")
    assert balances["traditional"] == Decimal("5000.00")
    assert balances["roth"] == Decimal("3000.00")


# ── Engine: ordered, taxed distribution ────────────────────────────────────

def _sim(session, **kw):
    svc = AnalysisService(session)
    defaults = dict(
        label="t",
        allocation={"us_equity": 0.6, "fixed_income": 0.4},
        initial_value=2_000_000.0,
        years=0,                     # retire immediately → deterministic start value
        num_simulations=3000,
        goal_amount=None,
        withdrawal_rate=5.0,
        withdrawal_years=30,
        current_age=60, retirement_age=60, ssa_claiming_age=67,
    )
    defaults.update(kw)
    return svc._simulate(**defaults)


def test_disabled_by_default(session):
    """Without the flag the result carries no income bridge (single pooled model)."""
    r = _sim(session, income_bridge=False)
    assert r.income_bridge is None


def test_bridge_populates_and_sums(session):
    r = _sim(session, income_bridge=True,
             bucket_balances={"taxable": 600_000, "traditional": 1_000_000, "roth": 400_000})
    ib = r.income_bridge
    assert ib is not None and ib.enabled
    # Start balances are proportional and sum to the retirement-date portfolio.
    starts = {b.bucket: float(b.start_balance) for b in ib.buckets}
    assert starts["taxable"] == 600_000 and starts["traditional"] == 1_000_000 and starts["roth"] == 400_000
    assert abs(sum(starts.values()) - float(ib.total_start)) < 1.0


def test_draw_order_taxable_first(session):
    """Year 1 is funded from the taxable bucket only; tax-deferred is tapped later."""
    r = _sim(session, income_bridge=True,
             bucket_balances={"taxable": 500_000, "traditional": 1_000_000, "roth": 500_000})
    fy = r.income_bridge.funding_by_year
    assert float(fy[0].taxable) > 0
    assert float(fy[0].traditional) == 0 and float(fy[0].roth) == 0
    # Once taxable is exhausted, a later year draws from the tax-deferred bucket.
    assert any(float(y.traditional) > 0 for y in fy)


def test_deferred_costs_more_than_roth(session):
    """Same balances, all tax-deferred vs all-Roth: the deferred plan pays tax,
    so it depletes faster and survives less often."""
    deferred = _sim(session, income_bridge=True, deferred_tax_rate=0.25,
                    bucket_balances={"taxable": 0, "traditional": 1_000_000, "roth": 0})
    roth = _sim(session, income_bridge=True,
                bucket_balances={"taxable": 0, "traditional": 0, "roth": 1_000_000})
    assert float(deferred.income_bridge.taxes_total_median) > 0
    assert float(roth.income_bridge.taxes_total_median) == 0
    assert deferred.portfolio_survival_rate < roth.portfolio_survival_rate


def test_ssa_reduces_taxes_and_improves_survival(session):
    """Social Security covers part of the need, so the portfolio draws (and is
    taxed) less and survives more."""
    base = dict(income_bridge=True,
                bucket_balances={"taxable": 300_000, "traditional": 1_200_000, "roth": 0})
    without = _sim(session, **base)
    with_ssa = _sim(session, ssa_annual_benefit=50_000.0, **base)
    assert with_ssa.portfolio_survival_rate > without.portfolio_survival_rate
    assert float(with_ssa.income_bridge.taxes_total_median) < float(without.income_bridge.taxes_total_median)


def test_longer_bridge_needs_more_capital(session):
    """A later claiming age means more pre-SSA years and a larger bridge number."""
    short = _sim(session, income_bridge=True, ssa_claiming_age=62, ssa_annual_benefit=30_000.0,
                 bucket_balances={"taxable": 800_000, "traditional": 1_000_000, "roth": 200_000})
    long = _sim(session, income_bridge=True, ssa_claiming_age=70, ssa_annual_benefit=30_000.0,
                bucket_balances={"taxable": 800_000, "traditional": 1_000_000, "roth": 200_000})
    assert short.income_bridge.bridge_years == 2
    assert long.income_bridge.bridge_years == 10
    assert float(long.income_bridge.bridge_number) > float(short.income_bridge.bridge_number)


def test_empty_buckets_fall_back_to_pooled(session):
    """Bridge requested but no bucket balances → no bridge, pooled model runs."""
    r = _sim(session, income_bridge=True, bucket_balances={"taxable": 0, "traditional": 0, "roth": 0})
    assert r.income_bridge is None
    assert r.portfolio_survival_rate is not None

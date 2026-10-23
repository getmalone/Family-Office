"""Regression tests for the tax fixes from the calculation audit (part B).

Covers wash sales (buy-after-sale trigger, partial matching, holding-period
tacking, IRA replacements, no double counting), Schedule D netting, taxable-
account filtering, NIIT on short-term gains, the $3,000 loss deduction,
per-lot harvest rates, the cost-basis method comparison, holding period
measured to the sale date, and refusing to oversell.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum
from app.models.tax_lot import TaxLot
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService

TODAY = date.today()
D = Decimal


class FakePrices:
    def __init__(self, prices):
        self.prices = prices

    def get_current_price(self, asset):
        return self.prices.get(asset.symbol)


def _setup(session, taxable=True):
    acct = Account(name="Brokerage", account_type=AccountTypeEnum.BROKERAGE,
                   institution="T", is_taxable=taxable)
    ira = Account(name="IRA", account_type=AccountTypeEnum.IRA_TRADITIONAL,
                  institution="T", is_taxable=False)
    asset = Asset(symbol="XYZ", name="XYZ Fund", asset_class=AssetClassEnum.US_EQUITY,
                  is_publicly_traded=True)
    session.add_all([acct, ira, asset])
    session.flush()
    return acct, ira, asset


def _days(n):
    return TODAY - timedelta(days=n)


def _tax(session, prices=None):
    return TaxService(session, market_data=FakePrices(prices or {}))


# ── Wash sales ───────────────────────────────────────────────────────────────

def test_buy_after_loss_sale_is_a_wash_sale(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=_days(400))
    _, (disp,) = ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=_days(20))
    ps.execute_buy(acct.id, a.id, D(100), D(81), transaction_date=_days(10))
    assert disp.is_wash_sale and disp.wash_sale_disallowed == D("2000.00")
    repl = session.query(TaxLot).filter(TaxLot.is_closed == False).one()
    assert repl.cost_basis_per_unit == D(101)                 # 81 + 20 disallowed/share
    # Holding period tacks on: 400-20=380 days held + 10 days → long-term now.
    assert repl.holding_start == _days(10) - timedelta(days=380)
    assert repl.is_long_term


def test_partial_replacement_disallows_only_matched_shares(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=_days(100))
    ps.execute_buy(acct.id, a.id, D(10), D(80), transaction_date=_days(15))     # replacement
    _, disposals = ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=_days(5))
    loss = next(d for d in disposals if d.realized_gain_loss < 0)
    assert loss.wash_sale_disallowed == D("200.00")             # 10 shares × $20, not $2,000
    repl = session.query(TaxLot).filter(TaxLot.is_closed == False).one()
    assert repl.cost_basis_per_unit == D(100)                   # 80 + 20


def test_replacement_lot_split_when_larger_than_loss(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(10), D(100), transaction_date=_days(100))
    ps.execute_sell(acct.id, a.id, D(10), D(80), transaction_date=_days(20))
    ps.execute_buy(acct.id, a.id, D(50), D(81), transaction_date=_days(10))
    open_lots = session.query(TaxLot).filter(TaxLot.is_closed == False).order_by(TaxLot.id).all()
    by_qty = {lot.remaining_quantity: lot for lot in open_lots}
    assert by_qty[D(10)].cost_basis_per_unit == D(101)          # matched shares carry the loss
    assert by_qty[D(40)].cost_basis_per_unit == D(81)           # the rest keep their own basis
    assert not by_qty[D(40)].is_wash_sale_adjusted


def test_ira_replacement_disallows_loss_without_basis_add(session):
    acct, ira, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=_days(100))
    _, (disp,) = ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=_days(20))
    ps.execute_buy(ira.id, a.id, D(100), D(81), transaction_date=_days(10))
    assert disp.wash_sale_disallowed == D("2000.00")
    ira_lot = session.query(TaxLot).filter(TaxLot.account_id == ira.id).one()
    assert ira_lot.cost_basis_per_unit == D(81)


def test_wash_loss_not_counted_twice(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=date(TODAY.year, 1, 2))
    ps.execute_sell(acct.id, a.id, D(100), D(120), transaction_date=date(TODAY.year, 1, 3))  # +2,000 ST
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=date(TODAY.year, 1, 4))
    ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=date(TODAY.year, 1, 5))   # −2,000
    ps.execute_buy(acct.id, a.id, D(100), D(80), transaction_date=date(TODAY.year, 1, 6))   # wash
    ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=date(TODAY.year, 1, 7))  # −2,000 (basis 100)
    s = _tax(session).get_tax_summary(TODAY.year)
    assert s.net_gain_loss == D(0)                              # one $2,000 loss, not two
    assert s.wash_sale_disallowed == D("2000.00")


# ── Holding period & overselling ─────────────────────────────────────────────

def test_holding_period_measured_to_sale_date(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(10), D(100), transaction_date=_days(400))
    _, (disp,) = ps.execute_sell(acct.id, a.id, D(10), D(110), transaction_date=_days(100))
    assert disp.is_short_term                                   # held 300 days at sale


def test_one_year_anniversary_across_leap_day():
    lot = TaxLot(acquisition_date=date(2023, 3, 1))
    assert not lot.is_long_term_on(date(2024, 3, 1))            # exactly one year: short-term
    assert lot.is_long_term_on(date(2024, 3, 2))
    leap = TaxLot(acquisition_date=date(2024, 2, 29))
    assert not leap.is_long_term_on(date(2025, 2, 28))
    assert leap.is_long_term_on(date(2025, 3, 1))


def test_cannot_sell_more_than_held(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=_days(10))
    with pytest.raises(ValueError):
        ps.execute_sell(acct.id, a.id, D(150), D(100))


# ── Tax summary ─────────────────────────────────────────────────────────────

def _realize(session, acct, a, days_held, buy, sell, qty=D(100)):
    ps = PortfolioService(session)
    sale = date(TODAY.year, 1, 15)
    ps.execute_buy(acct.id, a.id, qty, D(buy), transaction_date=sale - timedelta(days=days_held))
    ps.execute_sell(acct.id, a.id, qty, D(sell), transaction_date=sale)


def test_long_term_loss_offsets_short_term_gain(session):
    acct, _, a = _setup(session)
    b = Asset(symbol="ABC", name="ABC", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add(b)
    session.flush()
    _realize(session, acct, a, 500, 200, 100)                  # LT −10,000
    _realize(session, acct, b, 30, 100, 200)                   # ST +10,000
    s = _tax(session).get_tax_summary(TODAY.year)
    assert s.net_gain_loss == D(0)
    assert s.estimated_tax_liability == D("0.00")              # was 3,700


def test_ira_gains_are_not_taxed(session):
    _, ira, a = _setup(session)
    _realize(session, ira, a, 500, 100, 200)                   # +10,000 LT inside an IRA
    s = _tax(session).get_tax_summary(TODAY.year)
    assert s.long_term_gains == D(0) and s.estimated_tax_liability == D("0.00")


def test_short_term_gains_include_niit(session):
    acct, _, a = _setup(session)
    _realize(session, acct, a, 30, 100, 200)                   # ST +10,000
    s = _tax(session).get_tax_summary(TODAY.year)
    assert s.estimated_tax_liability == D("4080.00")           # 37% + 3.8%


def test_capital_loss_deduction_and_carryforward(session):
    acct, _, a = _setup(session)
    _realize(session, acct, a, 30, 200, 100)                   # ST −10,000
    s = _tax(session).get_tax_summary(TODAY.year)
    assert s.capital_loss_deduction == D(3000)
    assert s.loss_carryforward == D(7000)
    assert s.estimated_tax_liability == D("0.00")


# ── Harvesting & cost-basis comparison ──────────────────────────────────────

def test_harvest_savings_use_each_lots_rate(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(200), transaction_date=_days(500))   # LT loss 10,000
    ps.execute_buy(acct.id, a.id, D(1), D(50), transaction_date=_days(40))       # ST gain lot
    (cand,) = _tax(session, {"XYZ": D(100)}).find_harvest_candidates()
    assert cand.estimated_tax_savings == D("2380.00")          # 20% + 3.8%, not 40.8%
    assert not cand.is_short_term


def test_cost_basis_methods_differ(session):
    acct, _, a = _setup(session)
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(50), transaction_date=_days(800))
    ps.execute_buy(acct.id, a.id, D(100), D(150), transaction_date=_days(400))
    results = {r.scenario_name: r for r in
               _tax(session, {"XYZ": D(120)}).simulate_election("cost_basis_method")}
    # Selling 50 of 200 shares: FIFO picks the $50 lot (gain), HIFO/LIFO the $150 lot (loss).
    assert results["FIFO"].estimated_tax_impact == D("833.00")  # 50 × 70 × 23.8%
    assert results["Highest Cost First"].estimated_tax_impact == D("0.00")
    assert results["Highest Cost First"].recommended or results["LIFO"].recommended
    assert not results["FIFO"].recommended


def test_schedule_d_excludes_ira_and_applies_wash_adjustment(session):
    import json
    from app.services.reporting_service import ReportingService
    acct, ira, a = _setup(session)
    _realize(session, ira, a, 500, 100, 200)                   # IRA sale: not reported
    ps = PortfolioService(session)
    ps.execute_buy(acct.id, a.id, D(100), D(100), transaction_date=date(TODAY.year, 1, 2))
    ps.execute_sell(acct.id, a.id, D(100), D(80), transaction_date=date(TODAY.year, 1, 5))
    ps.execute_buy(acct.id, a.id, D(100), D(80), transaction_date=date(TODAY.year, 1, 6))
    content = json.loads(ReportingService(session).generate_schedule_d_data(TODAY.year).content_json)
    (row,) = content["form_8949"]
    assert row["wash_sale_adjustment"] == "2000.00" and row["adjusted_gain_loss"] == "0.00"
    assert content["totals"]["short_term_loss"] == "0"

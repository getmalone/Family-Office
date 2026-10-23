"""Regression tests for the risk & performance fixes from the calculation audit
(part C): date-aligned returns, deterministic metrics for holdings without
history, drawdown from the starting value, risk contributions summing to 100%,
per-security concentration, honest trailing returns, and value history that
includes cash and doesn't jump when a price series starts.
"""

from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot
from app.models.transaction import Transaction, TransactionTypeEnum
from app.services.analysis_service import AnalysisService
from app.services.portfolio_service import PortfolioService

TODAY = date.today()


def _weekdays(n):
    """The last n weekdays ending today (or the last weekday before today)."""
    out, d = [], TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _account(session, name="Brokerage"):
    acct = Account(name=name, account_type=AccountTypeEnum.BROKERAGE, institution="T", is_taxable=True)
    session.add(acct)
    session.flush()
    return acct


def _asset(session, sym, cls=AssetClassEnum.US_EQUITY, prices=None, dates=None):
    a = Asset(symbol=sym, name=sym, asset_class=cls, is_publicly_traded=True)
    session.add(a)
    session.flush()
    if prices is not None:
        for d, px in zip(dates, prices):
            session.add(AssetPrice(asset_id=a.id, price_date=d, close_price=Decimal(str(round(px, 6))),
                                   source="test"))
    session.flush()
    return a


def _lot(session, acct, asset, qty, cost):
    txn = Transaction(account_id=acct.id, asset_id=asset.id, transaction_type=TransactionTypeEnum.BUY,
                      transaction_date=date(2020, 1, 2), quantity=Decimal(qty),
                      price_per_unit=Decimal(cost), total_amount=Decimal(qty) * Decimal(cost),
                      fees=Decimal("0"))
    session.add(txn)
    session.flush()
    session.add(TaxLot(account_id=acct.id, asset_id=asset.id, acquisition_date=date(2020, 1, 2),
                       acquisition_transaction_id=txn.id, original_quantity=Decimal(qty),
                       remaining_quantity=Decimal(qty), cost_basis_per_unit=Decimal(cost),
                       original_cost_basis_per_unit=Decimal(cost)))
    session.flush()


def _walk(n, seed, vol=0.01):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0.0004, vol, n))


# ── Date alignment ──────────────────────────────────────────────────────────

def test_missing_day_does_not_shift_returns(session):
    dates = _weekdays(200)
    px = _walk(200, 1)
    acct = _account(session)
    a = _asset(session, "AAA", prices=px, dates=dates)
    gap = [i for i in range(200) if i != 100]                       # one missing day mid-window
    b = _asset(session, "BBB", prices=px[gap], dates=[dates[i] for i in gap])
    _lot(session, acct, a, 10, 100)
    _lot(session, acct, b, 10, 100)
    m = AnalysisService(session).get_correlation_matrix(lookback_days=180)
    assert m.symbols == ["AAA", "BBB"]
    assert float(m.matrix[0][1]) > 0.999                            # was 0.40 with position alignment


def test_weekend_snapshot_rows_are_ignored(session):
    dates = _weekdays(120)
    px = _walk(120, 2)
    a = _asset(session, "AAA", prices=px, dates=dates)
    sat = next(d for d in (TODAY - timedelta(days=k) for k in range(1, 9)) if d.weekday() == 5)
    session.add(AssetPrice(asset_id=a.id, price_date=sat, close_price=Decimal("1"), source="snap"))
    session.flush()
    ser = AnalysisService(session)._bulk_price_series(["AAA"], dates[0], TODAY)["AAA"]
    assert sat not in ser.index


# ── Risk metrics ────────────────────────────────────────────────────────────

def _risk_portfolio(session):
    dates = _weekdays(260)
    acct = _account(session)
    voo = _asset(session, "VOO", prices=_walk(260, 3), dates=dates)
    bnd = _asset(session, "BND", AssetClassEnum.FIXED_INCOME, prices=_walk(260, 4, 0.003), dates=dates)
    pe = _asset(session, "PRIV", AssetClassEnum.PRIVATE_EQUITY)          # no price history
    session.add(AssetPrice(asset_id=pe.id, price_date=TODAY, close_price=Decimal("100"), source="manual"))
    _lot(session, acct, voo, 500, 100)
    _lot(session, acct, bnd, 300, 100)
    _lot(session, acct, pe, 200, 100)
    return acct


def test_risk_metrics_are_deterministic(session):
    _risk_portfolio(session)
    svc = AnalysisService(session)
    r1, r2 = svc.get_risk_metrics(), svc.get_risk_metrics()
    assert (r1.portfolio_volatility, r1.sharpe_ratio, r1.max_drawdown, r1.beta) == \
           (r2.portfolio_volatility, r2.sharpe_ratio, r2.max_drawdown, r2.beta)
    assert r1.history_coverage_pct is not None and float(r1.history_coverage_pct) < 100


def test_risk_contributions_sum_to_100(session):
    _risk_portfolio(session)
    r = AnalysisService(session).get_risk_metrics()
    assert abs(sum(float(p.risk_contribution_pct) for p in r.position_risks) - 100) < 0.05
    assert abs(sum(float(a.contribution_to_risk) for a in r.asset_class_risks) - 100) < 0.05


def test_drawdown_counts_a_first_day_loss(session):
    dates = _weekdays(60)
    px = [100.0, 90.0, 85.5] + [85.5] * 57                          # −10%, −5%, then flat
    acct = _account(session)
    a = _asset(session, "AAA", prices=px, dates=dates)
    _lot(session, acct, a, 10, 100)
    r = AnalysisService(session).get_risk_metrics(lookback_days=63)
    assert float(r.max_drawdown) == -14.5                           # was −5.0


def test_concentration_merges_accounts(session):
    a1, a2 = _account(session, "A"), _account(session, "B")
    vti = _asset(session, "VTI")
    bnd = _asset(session, "BND", AssetClassEnum.FIXED_INCOME)
    for a in (vti, bnd):
        session.add(AssetPrice(asset_id=a.id, price_date=TODAY, close_price=Decimal("100"), source="t"))
    _lot(session, a1, vti, 25, 100)
    _lot(session, a2, vti, 25, 100)
    _lot(session, a1, bnd, 50, 100)
    c = AnalysisService(session).get_risk_metrics().concentration
    assert float(c.hhi_index) == 5000.0                             # was 3,750
    assert float(c.largest_position_weight_pct) == 50.0


# ── Trailing returns ────────────────────────────────────────────────────────

def test_cash_dilutes_trailing_return(session):
    dates = [TODAY - timedelta(days=k) for k in range(400, -1, -1) if (TODAY - timedelta(days=k)).weekday() < 5]
    px = list(np.linspace(100, 130, len(dates)))
    acct = _account(session)
    voo = _asset(session, "VOO", prices=px, dates=dates)
    cash = _asset(session, "CASH", AssetClassEnum.CASH)
    _lot(session, acct, voo, 100, 100)                              # ≈ $13k
    _lot(session, acct, cash, 117000, 1)                            # ≈ 90% cash
    r = AnalysisService(session).get_return_metrics()
    assert r.twr_1yr is not None and 0 < float(r.twr_1yr) < 5      # VOO alone ≈ +22%


def test_short_history_not_annualised_and_does_not_blank_others(session):
    long_dates = [TODAY - timedelta(days=k) for k in range(400, -1, -1)]
    acct = _account(session)
    voo = _asset(session, "VOO", prices=list(np.linspace(100, 110, len(long_dates))), dates=long_dates)
    new = _asset(session, "NEW", prices=list(np.linspace(100, 120, 35)), dates=long_dates[-35:])
    _lot(session, acct, voo, 990, 100)
    _lot(session, acct, new, 10, 100)                               # ~1% position, 35 days of history
    r = AnalysisService(session).get_return_metrics()
    assert r.twr_1yr is not None                                    # was None for the whole portfolio
    assert float(r.history_coverage_pct) < 100


# ── Value history ───────────────────────────────────────────────────────────

def test_history_includes_cash_and_backfills_late_prices(session):
    dates = _weekdays(10)
    acct = _account(session)
    voo = _asset(session, "VOO", prices=[100] * 10, dates=dates)
    qqq = _asset(session, "QQQ", prices=[100] * 3, dates=dates[-3:])  # first priced 3 days ago
    cash = _asset(session, "CASH", AssetClassEnum.CASH)
    _lot(session, acct, voo, 100, 100)
    _lot(session, acct, qqq, 100, 100)
    _lot(session, acct, cash, 50000, 1)
    hist = PortfolioService(session).get_portfolio_history(days=30)
    assert hist["total"] == [70000.0] * len(hist["total"])          # was 10k, then 20k

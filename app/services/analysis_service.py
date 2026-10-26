"""
Financial analysis service for the Family Office.

Provides portfolio risk metrics (volatility, Sharpe, drawdown, beta),
investment profile management with drift/rebalancing analysis, and
Monte Carlo simulation for projecting portfolio trajectories under
different allocation strategies.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.asset import Asset, AssetPrice
from app.models.investment_profile import InvestmentProfile, RiskToleranceEnum
from app.models.tax_lot import TaxLot
from app.schemas.analysis import (
    AllocationTarget,
    AssetClassRisk,
    BridgeBucket,
    BridgeYearFunding,
    ConcentrationRisk,
    CorrelationMatrix,
    DriftAnalysis,
    IncomeBridge,
    InvestmentProfileSchema,
    LookthroughPosition,
    MonteCarloComparison,
    MonteCarloResult,
    PositionRisk,
    RebalanceAction,
    SsaHousehold,
    SsaPhase,
    SsaPerson,
    RegimeState,
    ReturnMetrics,
    RiskMetrics,
    SimulationOutcome,
)
from app.services.market_data import MarketDataService, STABLE_VALUE_SYMBOLS
from app.services.portfolio_service import PortfolioService
from app.services.render_guard import network_allowed
from app.services.symbols import yahoo_symbol

# Process-lifetime cache for trailing-window returns, refreshed hourly (the
# dashboard's landing-page call must stay snappy; period returns barely move
# intraday). Keyed by "YYYYMMDDHH".
_PERIOD_RETURNS_CACHE: dict[str, list[dict]] = {}


def invalidate_period_returns_cache() -> None:
    """Drop the trailing-window cache so the next dashboard load recomputes —
    called after a price-history backfill fills in new dates."""
    _PERIOD_RETURNS_CACHE.clear()


# Assumed annual return and volatility per asset class (used for private assets
# without price history, and as fallback for Monte Carlo simulation).
ASSET_CLASS_ASSUMPTIONS: dict[str, dict[str, float]] = {
    "us_equity":       {"return": 0.10, "volatility": 0.16},
    "intl_equity":     {"return": 0.08, "volatility": 0.18},
    "fixed_income":    {"return": 0.04, "volatility": 0.05},
    "real_estate":     {"return": 0.07, "volatility": 0.12},
    "private_equity":  {"return": 0.12, "volatility": 0.22},
    "venture_capital": {"return": 0.15, "volatility": 0.30},
    "hedge_fund":      {"return": 0.08, "volatility": 0.10},
    "commodity":       {"return": 0.05, "volatility": 0.20},
    "crypto":          {"return": 0.15, "volatility": 0.60},
    "cash":            {"return": 0.04, "volatility": 0.005},
    "alternative":     {"return": 0.07, "volatility": 0.15},
    "other":           {"return": 0.06, "volatility": 0.12},
}

# Long-run correlations between asset classes (annual returns), used to combine
# per-class volatilities into portfolio volatility: σ_p² = wᵀ Σ w. Treating the
# classes as uncorrelated made equity-heavy mixes look far safer than they are
# (US/international stocks move together ~0.85). Order matches the keys above;
# missing pairs default to 0. The matrix is checked to be positive semi-definite.
_CORR_ORDER = list(ASSET_CLASS_ASSUMPTIONS)
_CORR_PAIRS: dict[tuple[str, str], float] = {
    ("us_equity", "intl_equity"): 0.85, ("us_equity", "fixed_income"): 0.10,
    ("us_equity", "real_estate"): 0.65, ("us_equity", "private_equity"): 0.75,
    ("us_equity", "venture_capital"): 0.60, ("us_equity", "hedge_fund"): 0.65,
    ("us_equity", "commodity"): 0.30, ("us_equity", "crypto"): 0.35,
    ("us_equity", "alternative"): 0.50, ("us_equity", "other"): 0.60,
    ("intl_equity", "fixed_income"): 0.10, ("intl_equity", "real_estate"): 0.60,
    ("intl_equity", "private_equity"): 0.70, ("intl_equity", "venture_capital"): 0.55,
    ("intl_equity", "hedge_fund"): 0.60, ("intl_equity", "commodity"): 0.40,
    ("intl_equity", "crypto"): 0.30, ("intl_equity", "alternative"): 0.50,
    ("intl_equity", "other"): 0.60,
    ("fixed_income", "real_estate"): 0.25, ("fixed_income", "hedge_fund"): 0.10,
    ("fixed_income", "cash"): 0.10, ("fixed_income", "alternative"): 0.20,
    ("fixed_income", "other"): 0.30,
    ("real_estate", "private_equity"): 0.55, ("real_estate", "venture_capital"): 0.45,
    ("real_estate", "hedge_fund"): 0.45, ("real_estate", "commodity"): 0.30,
    ("real_estate", "crypto"): 0.25, ("real_estate", "alternative"): 0.50,
    ("real_estate", "other"): 0.50,
    ("private_equity", "venture_capital"): 0.75, ("private_equity", "hedge_fund"): 0.55,
    ("private_equity", "commodity"): 0.25, ("private_equity", "crypto"): 0.30,
    ("private_equity", "alternative"): 0.50, ("private_equity", "other"): 0.50,
    ("venture_capital", "hedge_fund"): 0.45, ("venture_capital", "commodity"): 0.20,
    ("venture_capital", "crypto"): 0.35, ("venture_capital", "alternative"): 0.45,
    ("venture_capital", "other"): 0.45,
    ("hedge_fund", "commodity"): 0.30, ("hedge_fund", "crypto"): 0.30,
    ("hedge_fund", "alternative"): 0.50, ("hedge_fund", "other"): 0.45,
    ("commodity", "crypto"): 0.20, ("commodity", "alternative"): 0.30,
    ("commodity", "other"): 0.30,
    ("crypto", "alternative"): 0.25, ("crypto", "other"): 0.30,
    ("alternative", "other"): 0.50,
}


def asset_class_correlation(a: str, b: str) -> float:
    """Assumed long-run correlation between two asset classes (unknown → 'other')."""
    a = a if a in ASSET_CLASS_ASSUMPTIONS else "other"
    b = b if b in ASSET_CLASS_ASSUMPTIONS else "other"
    if a == b:
        return 1.0
    return _CORR_PAIRS.get((a, b), _CORR_PAIRS.get((b, a), 0.0))


def portfolio_return_and_vol(allocation: dict[str, float]) -> tuple[float, float]:
    """Expected simple annual return and volatility of an asset-class mix.

    Weights are normalised; volatility uses the correlation table above
    (σ_p² = Σᵢ Σⱼ wᵢ wⱼ σᵢ σⱼ ρᵢⱼ). An empty mix falls back to 4% / 5%.
    """
    items = [(ac, wt) for ac, wt in allocation.items() if wt > 0]
    if not items:
        return 0.04, 0.05
    classes = [ac for ac, _ in items]
    w = np.array([wt for _, wt in items], dtype=float)
    w = w / w.sum()
    params = [ASSET_CLASS_ASSUMPTIONS.get(ac, ASSET_CLASS_ASSUMPTIONS["other"]) for ac in classes]
    mu = np.array([p["return"] for p in params])
    sig = np.array([p["volatility"] for p in params])
    corr = np.array([[asset_class_correlation(a, b) for b in classes] for a in classes])
    cov = np.outer(sig, sig) * corr
    return float(w @ mu), float(np.sqrt(max(float(w @ cov @ w), 0.0)))


def budget_totals(items: list[dict] | None) -> tuple[float, float]:
    """(fixed, variable) annual spending in today's dollars from itemised
    monthly categories: [{"name": ..., "monthly": 1234.5, "fixed": True}, ...]."""
    fixed = variable = 0.0
    for it in items or []:
        try:
            monthly = max(0.0, float(it.get("monthly") or 0))
        except (TypeError, ValueError):
            continue
        if it.get("fixed"):
            fixed += monthly * 12
        else:
            variable += monthly * 12
    return fixed, variable


def _spending_kwargs(inflation_rate: float, budget_items: list[dict] | None,
                     reinvest_surplus: bool = False) -> dict:
    """_simulate kwargs for inflation, surplus handling and (when a budget is
    given) budget mode."""
    out: dict = {"inflation_rate": max(0.0, float(inflation_rate or 0.0)),
                 "reinvest_surplus": bool(reinvest_surplus)}
    fixed, variable = budget_totals(budget_items)
    if fixed + variable > 0:
        out.update(spending_fixed_annual=fixed, spending_variable_annual=variable)
    return out

# Default target allocations for the 5 system profiles.
DEFAULT_PROFILES: list[dict] = [
    {
        "name": "Conservative",
        "risk_tolerance": "conservative",
        "description": "Capital preservation focus. Heavy fixed income and cash, minimal equity exposure.",
        "allocations": {
            "us_equity": 15, "intl_equity": 5, "fixed_income": 50,
            "real_estate": 5, "commodity": 0, "cash": 20, "alternative": 5,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Moderate",
        "risk_tolerance": "moderate",
        "description": "Balanced income and growth. Tilted toward bonds with meaningful equity allocation.",
        "allocations": {
            "us_equity": 25, "intl_equity": 10, "fixed_income": 35,
            "real_estate": 10, "commodity": 5, "cash": 10, "alternative": 5,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Balanced",
        "risk_tolerance": "balanced",
        "is_active": True,  # default active profile on a fresh database
        "description": "Equal emphasis on growth and stability. Diversified across major asset classes.",
        "allocations": {
            "us_equity": 35, "intl_equity": 15, "fixed_income": 25,
            "real_estate": 10, "commodity": 5, "cash": 5, "alternative": 5,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Growth",
        "risk_tolerance": "growth",
        "description": "Growth-oriented with higher equity allocation. Accepts more volatility for higher returns.",
        "allocations": {
            "us_equity": 45, "intl_equity": 20, "fixed_income": 15,
            "real_estate": 8, "commodity": 3, "cash": 2, "alternative": 2,
            "crypto": 3, "private_equity": 2, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Aggressive",
        "risk_tolerance": "aggressive",
        "description": "Maximum growth potential. Heavy equity and alternatives with minimal bonds and cash.",
        "allocations": {
            "us_equity": 40, "intl_equity": 20, "fixed_income": 5,
            "real_estate": 5, "commodity": 3, "cash": 0, "alternative": 2,
            "crypto": 10, "private_equity": 8, "venture_capital": 5,
            "hedge_fund": 2, "other": 0,
        },
    },
    # ── Strategy-based profiles (common investment strategies) ───────────────
    {
        "name": "Income",
        "risk_tolerance": "conservative",
        "description": "Retirement income & capital preservation. High-quality bonds, dividend equity, and cash for steady yield.",
        "allocations": {
            "us_equity": 15, "intl_equity": 5, "fixed_income": 45,
            "real_estate": 10, "commodity": 0, "cash": 20, "alternative": 5,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "60/40 Classic",
        "risk_tolerance": "balanced",
        "description": "The classic 60/40 portfolio — 60% diversified equities, 40% high-quality bonds.",
        "allocations": {
            "us_equity": 40, "intl_equity": 20, "fixed_income": 40,
            "real_estate": 0, "commodity": 0, "cash": 0, "alternative": 0,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Three-Fund Index",
        "risk_tolerance": "growth",
        "description": "Low-cost Boglehead three-fund: total US equity, total international equity, and total bond market.",
        "allocations": {
            "us_equity": 50, "intl_equity": 20, "fixed_income": 30,
            "real_estate": 0, "commodity": 0, "cash": 0, "alternative": 0,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "All-Weather (Risk Parity)",
        "risk_tolerance": "moderate",
        "description": "Ray Dalio-inspired all-weather mix engineered to hold up across growth and inflation regimes.",
        "allocations": {
            "us_equity": 18, "intl_equity": 12, "fixed_income": 55,
            "real_estate": 5, "commodity": 10, "cash": 0, "alternative": 0,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Permanent Portfolio",
        "risk_tolerance": "conservative",
        "description": "Harry Browne's permanent portfolio: equal split of stocks, long bonds, cash, and gold (commodities).",
        "allocations": {
            "us_equity": 25, "intl_equity": 0, "fixed_income": 25,
            "real_estate": 0, "commodity": 25, "cash": 25, "alternative": 0,
            "crypto": 0, "private_equity": 0, "venture_capital": 0,
            "hedge_fund": 0, "other": 0,
        },
    },
    {
        "name": "Endowment (Yale-Style)",
        "risk_tolerance": "aggressive",
        "description": "Yale-model endowment: heavy private markets and real assets, low public bonds. For long horizons with illiquidity tolerance.",
        "allocations": {
            "us_equity": 15, "intl_equity": 10, "fixed_income": 10,
            "real_estate": 10, "commodity": 5, "cash": 0, "alternative": 5,
            "crypto": 0, "private_equity": 20, "venture_capital": 10,
            "hedge_fund": 15, "other": 0,
        },
    },
]


class AnalysisService:
    """
    Financial analysis for the Family Office.

    Computes risk metrics, manages investment profiles, performs drift analysis
    with rebalancing recommendations, and runs Monte Carlo simulations to project
    portfolio outcomes under different allocation strategies.
    """

    def __init__(self, session: Session, market_data: MarketDataService | None = None):
        self.session = session
        self.market_data = market_data or MarketDataService(session)
        self.portfolio_svc = PortfolioService(session, self.market_data)

    # ── Risk Analysis ───────────────────────────────────────────────────

    def get_risk_metrics(
        self, lookback_days: int = 252, risk_free_rate: float = 0.05
    ) -> RiskMetrics:
        """Compute portfolio risk metrics.

        ``lookback_days`` is in trading days (252 ≈ 1 year). Returns of holdings
        with price history are aligned by date. Volatility, Sharpe, beta and
        risk contributions use a full annual covariance matrix: sample
        covariance among holdings with history, and the asset-class assumptions
        (volatility × the asset-class correlation table) for holdings without
        it — deterministic, and contributions sum to 100%. Drawdown and VaR use
        the dated history of covered holdings, rescaled to the whole
        portfolio's volatility.
        """
        import math

        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        if total_mv <= 0:
            return self._empty_risk_metrics(lookback_days, risk_free_rate)

        end_date = date.today()
        start_date = end_date - timedelta(days=math.ceil(lookback_days * 365 / 252))

        # One entry per security: a fund held in several accounts is one position.
        positions: dict[int, dict] = {}
        for h in summary.holdings:
            pos = positions.setdefault(h.asset_id, {
                "asset_id": h.asset_id, "symbol": h.symbol, "name": h.name,
                "asset_class": h.asset_class, "mv": 0.0,
            })
            pos["mv"] += float(h.market_value)
        pos_list = [p for p in positions.values() if p["mv"] > 0]
        if not pos_list:
            return self._empty_risk_metrics(lookback_days, risk_free_rate)
        for pos in pos_list:
            pos["weight"] = pos["mv"] / total_mv

        symbols = sorted({p["symbol"] for p in pos_list if p["symbol"]})
        price_map = self._bulk_price_series(symbols + ["SPY"], start_date, end_date)
        spy_prices = price_map.pop("SPY", None) if "SPY" not in symbols else price_map.get("SPY")
        hist_syms = [sym for sym in symbols if sym in price_map]
        rets = self._aligned_returns({sym: price_map[sym] for sym in hist_syms})
        if len(rets) < 20:
            rets = rets.iloc[0:0]
            hist_syms = []

        n = len(pos_list)
        w = np.array([p["weight"] for p in pos_list])
        sig = np.empty(n)
        mu = np.empty(n)
        hist_idx: dict[int, str] = {}
        for i, pos in enumerate(pos_list):
            assumed = ASSET_CLASS_ASSUMPTIONS.get(pos["asset_class"], ASSET_CLASS_ASSUMPTIONS["other"])
            if pos["symbol"] in hist_syms:
                col = rets[pos["symbol"]].values
                hist_idx[i] = pos["symbol"]
                sig[i] = float(np.std(col, ddof=1) * np.sqrt(252))
                mu[i] = float(np.mean(col) * 252)
            else:
                sig[i] = assumed["volatility"]
                mu[i] = assumed["return"]

        # Annual covariance: sample among covered holdings, assumed elsewhere.
        cov = np.empty((n, n))
        sample_cov = (np.cov(rets.values, rowvar=False, ddof=1) * 252
                      if len(hist_syms) > 1 else None)
        col_of = {sym: j for j, sym in enumerate(rets.columns)}
        for i in range(n):
            for j in range(n):
                if i in hist_idx and j in hist_idx and sample_cov is not None:
                    cov[i, j] = sample_cov[col_of[hist_idx[i]], col_of[hist_idx[j]]]
                elif i == j:
                    cov[i, j] = sig[i] ** 2
                else:
                    rho = asset_class_correlation(pos_list[i]["asset_class"], pos_list[j]["asset_class"])
                    cov[i, j] = rho * sig[i] * sig[j]

        port_var = float(w @ cov @ w)
        port_vol = math.sqrt(max(port_var, 0.0))
        port_mean = float(w @ mu)
        sharpe = (port_mean - risk_free_rate) / port_vol if port_vol > 0 else 0.0
        marginal = cov @ w
        history_weight = float(sum(w[i] for i in hist_idx))

        # Beta vs SPY: sample beta for covered holdings when SPY history exists,
        # otherwise ρ(class, US equity) × σ_i / σ_SPY from the assumptions.
        spy_rets = None
        if spy_prices is not None and len(rets):
            joined = self._aligned_returns({"_p": price_map[hist_syms[0]], "SPY": spy_prices})
            spy_rets = joined["SPY"].reindex(rets.index).dropna()
        spy_vol = (float(np.std(spy_rets.values, ddof=1) * np.sqrt(252))
                   if spy_rets is not None and len(spy_rets) >= 20 else
                   ASSET_CLASS_ASSUMPTIONS["us_equity"]["volatility"])
        betas = np.empty(n)
        for i, pos in enumerate(pos_list):
            if i in hist_idx and spy_rets is not None and len(spy_rets) >= 20:
                r_i = rets[hist_idx[i]].reindex(spy_rets.index).values
                c = np.cov(r_i, spy_rets.values, ddof=1)
                betas[i] = c[0, 1] / c[1, 1] if c[1, 1] > 0 else 0.0
            else:
                betas[i] = asset_class_correlation(pos["asset_class"], "us_equity") * sig[i] / spy_vol
        port_beta = float(w @ betas)

        # Daily portfolio series for drawdown / VaR: covered holdings on their
        # common dates, rescaled so its volatility matches the whole portfolio's.
        port_daily = np.array([])
        if hist_idx:
            w_hist = np.array([w[i] for i in hist_idx]) / history_weight
            series = rets[[hist_idx[i] for i in hist_idx]].values @ w_hist
            hist_vol = float(np.std(series, ddof=1) * np.sqrt(252))
            port_daily = series * (port_vol / hist_vol) if hist_vol > 0 else series

        max_dd = 0.0
        dd_period = "insufficient price history"
        if len(port_daily):
            # Include the starting value (1.0) so a loss on day one counts.
            cumulative = np.concatenate([[1.0], np.cumprod(1 + port_daily)])
            running_max = np.maximum.accumulate(cumulative)
            drawdowns = cumulative / running_max - 1
            dd_end_idx = int(np.argmin(drawdowns))
            dd_start_idx = int(np.argmax(cumulative[:dd_end_idx + 1]))
            max_dd = float(drawdowns[dd_end_idx])
            # cumulative[k] is the value on the k-th common price date.
            import pandas as pd
            common = pd.DataFrame({sym: price_map[sym] for sym in hist_syms}).dropna().index
            dates = list(common)
            dd_period = f"{dates[dd_start_idx]} to {dates[dd_end_idx]}"

        # Per-position risk
        position_risks = []
        for i, pos in enumerate(pos_list):
            risk_contrib = (w[i] * marginal[i] / port_var * 100) if port_var > 0 else 0.0
            position_risks.append(PositionRisk(
                asset_id=pos["asset_id"],
                symbol=pos["symbol"],
                name=pos["name"],
                asset_class=pos["asset_class"],
                weight_pct=Decimal(str(round(pos["weight"] * 100, 2))),
                annualized_volatility=Decimal(str(round(sig[i] * 100, 2))),
                beta=Decimal(str(round(float(betas[i]), 3))),
                risk_contribution_pct=Decimal(str(round(float(risk_contrib), 2))),
            ))

        # Concentration — per security (accounts merged)
        sorted_weights = sorted([p["weight"] * 100 for p in pos_list], reverse=True)
        top5 = sum(sorted_weights[:5])
        top10 = sum(sorted_weights[:10])
        # HHI in DOJ convention: weights are expressed as percentages (0–100),
        # so a single-holding portfolio → 100² = 10,000 (maximum concentration).
        hhi = sum(x ** 2 for x in sorted_weights)
        largest = max(pos_list, key=lambda p: p["weight"])

        # Look-through HHI: expand each ETF/fund to its underlying securities.
        # For each holding use either its own symbol or a look_through_ticker
        # (e.g. an institutional pool class that mirrors a public fund like FCNTX).
        from app.models.asset import Asset as AssetModel
        lt_ticker_map: dict[str, str] = {}  # holding_symbol_or_id → lookup_ticker
        for h in summary.holdings:
            asset = self.session.get(AssetModel, h.asset_id)
            if asset and asset.look_through_ticker:
                # Use the fund's own symbol (or asset_id label) as key → public ticker
                key = h.symbol or f"asset_{h.asset_id}"
                lt_ticker_map[key] = asset.look_through_ticker

        # Build the full set of tickers to fetch from yfinance
        direct_syms = [h.symbol for h in summary.holdings if h.symbol]
        alias_syms   = list(lt_ticker_map.values())
        all_lookup_syms = list(set(direct_syms + alias_syms))
        fund_holdings_map = self._get_fund_holdings(all_lookup_syms)

        lt_hhi, lt_positions, lt_coverage, lt_top = self._compute_lookthrough_hhi(
            summary.holdings, fund_holdings_map, lt_ticker_map
        )

        concentration = ConcentrationRisk(
            top_5_weight_pct=Decimal(str(round(top5, 2))),
            top_10_weight_pct=Decimal(str(round(top10, 2))),
            largest_position=largest["symbol"] or largest["name"],
            largest_position_weight_pct=Decimal(str(round(largest["weight"] * 100, 2))),
            hhi_index=Decimal(str(round(hhi, 2))),
            hhi_lookthrough=Decimal(str(round(lt_hhi, 2))) if lt_hhi is not None else None,
            hhi_lookthrough_positions=lt_positions,
            hhi_lookthrough_coverage_pct=Decimal(str(round(lt_coverage, 1))) if lt_coverage is not None else None,
            hhi_lookthrough_top=lt_top,
        )

        # Asset class risk: weight, volatility and contribution from the same
        # covariance matrix (class contributions sum to 100% too).
        ac_risks = []
        ac_groups: dict[str, list[int]] = {}
        for i, pos in enumerate(pos_list):
            ac_groups.setdefault(pos["asset_class"], []).append(i)
        for ac, idx in ac_groups.items():
            ac_weight = float(sum(w[i] for i in idx))
            w_ac = np.zeros(n)
            w_ac[idx] = w[idx]
            ac_vol = math.sqrt(max(float(w_ac @ cov @ w_ac), 0.0)) / ac_weight if ac_weight > 0 else 0.0
            contrib = (float(w_ac @ marginal) / port_var * 100) if port_var > 0 else 0.0
            ac_risks.append(AssetClassRisk(
                asset_class=ac,
                weight_pct=Decimal(str(round(ac_weight * 100, 2))),
                annualized_volatility=Decimal(str(round(ac_vol * 100, 2))),
                contribution_to_risk=Decimal(str(round(contrib, 2))),
            ))

        # Value-at-Risk and CVaR (historical simulation on the dated series)
        sorted_rets = np.sort(port_daily)
        var_95 = cvar_95 = var_99 = cvar_99 = None
        if len(sorted_rets) >= 20:
            idx_95 = max(0, int(np.floor(0.05 * len(sorted_rets))))
            idx_99 = max(0, int(np.floor(0.01 * len(sorted_rets))))
            var_95 = Decimal(str(round(abs(float(sorted_rets[idx_95])) * total_mv, 2)))
            cvar_95 = Decimal(str(round(abs(float(np.mean(sorted_rets[:idx_95 + 1]))) * total_mv, 2)))
            var_99 = Decimal(str(round(abs(float(sorted_rets[idx_99])) * total_mv, 2)))
            cvar_99 = Decimal(str(round(abs(float(np.mean(sorted_rets[:idx_99 + 1]))) * total_mv, 2)))

        return RiskMetrics(
            portfolio_volatility=Decimal(str(round(port_vol * 100, 2))),
            sharpe_ratio=Decimal(str(round(sharpe, 3))),
            max_drawdown=Decimal(str(round(max_dd * 100, 2))),
            max_drawdown_period=dd_period,
            beta=Decimal(str(round(port_beta, 3))),
            risk_free_rate=Decimal(str(risk_free_rate)),
            lookback_days=lookback_days,
            var_95=var_95,
            cvar_95=cvar_95,
            var_99=var_99,
            cvar_99=cvar_99,
            position_risks=sorted(position_risks, key=lambda p: p.weight_pct, reverse=True),
            concentration=concentration,
            asset_class_risks=sorted(ac_risks, key=lambda a: a.weight_pct, reverse=True),
            history_coverage_pct=Decimal(str(round(history_weight * 100, 1))),
        )

    def _bulk_price_series(
        self, symbols: list[str], start_date: date, end_date: date, min_coverage: float = 0.6,
    ) -> "dict[str, pd.Series]":
        """Daily closing prices per symbol, indexed by date (DB first, then one
        bulk yfinance download in refresh contexts only).

        DB history is used when it covers at least ``min_coverage`` of the
        window's expected trading days (min 10 rows). Weekend rows of non-crypto
        assets are dropped — a weekend snapshot just repeats Friday's close and
        would add fake zero-return days. Series with a ±99% one-day move are
        treated as corrupt and skipped.
        """
        import pandas as pd

        if not symbols:
            return {}
        expected = max(1, (end_date - start_date).days) * 252 / 365
        min_rows = max(10, int(expected * min_coverage))
        results: dict[str, pd.Series] = {}

        from app.models.asset import Asset as AssetModel
        db_assets = {
            a.symbol: a
            for a in self.session.query(AssetModel).filter(AssetModel.symbol.in_(symbols)).all()
            if a.symbol
        }
        for sym, asset in db_assets.items():
            rows = (
                self.session.query(AssetPrice.price_date, AssetPrice.close_price)
                .filter(AssetPrice.asset_id == asset.id,
                        AssetPrice.price_date >= start_date,
                        AssetPrice.price_date <= end_date)
                .order_by(AssetPrice.price_date)
                .all()
            )
            is_crypto = str(getattr(asset.asset_class, "value", asset.asset_class)) == "crypto"
            pts = {d: float(px) for d, px in rows if is_crypto or d.weekday() < 5}
            if len(pts) < min_rows:
                continue
            ser = pd.Series(pts).sort_index()
            if (ser.pct_change().abs() > 0.99).any():
                continue
            results[sym] = ser

        # Bulk-download remaining symbols from yfinance — but ONLY in an
        # explicit-refresh context. During an ordinary page render we never hit
        # the network (it scales with portfolio size and would hang the page);
        # symbols without stored history simply count as "no history".
        # Fetch by the Yahoo-safe form (BRK/B → BRK-B) and skip symbols no feed
        # can resolve (CUSIPs, subtotal rows) — they can only fail noisily.
        missing_by_ysym: dict[str, list[str]] = {}
        for sym in symbols:
            if sym in results:
                continue
            ysym = yahoo_symbol(sym)
            if ysym is not None:
                missing_by_ysym.setdefault(ysym, []).append(sym)
        if missing_by_ysym and network_allowed():
            try:
                import yfinance as yf
                fetch_syms = list(missing_by_ysym)
                raw = yf.download(fetch_syms, start=str(start_date),
                                  end=str(end_date + timedelta(days=1)),   # end is exclusive
                                  auto_adjust=True, progress=False, threads=True)
                if not raw.empty:
                    close = raw["Close"] if "Close" in raw.columns else raw
                    cols = ({y: close[y] for y in missing_by_ysym if y in close.columns}
                            if hasattr(close, "columns") else
                            ({fetch_syms[0]: close} if len(fetch_syms) == 1 else {}))
                    for ysym, col in cols.items():
                        col = col.dropna()
                        if len(col) >= 10:
                            ser = pd.Series(col.values, index=[ts.date() for ts in col.index])
                            for sym in missing_by_ysym[ysym]:
                                results[sym] = ser
            except Exception:
                pass

        return results

    def _bulk_daily_returns(
        self, symbols: list[str], start_date: date, end_date: date
    ) -> dict[str, np.ndarray | None]:
        """Per-symbol simple daily returns (each series on its own dates).
        Combine several symbols with _aligned_returns, which joins on dates."""
        return {
            sym: np.diff(ser.values) / ser.values[:-1]
            for sym, ser in self._bulk_price_series(symbols, start_date, end_date).items()
        }

    def _aligned_returns(self, price_map: "dict[str, pd.Series]") -> "pd.DataFrame":
        """Simple daily returns of several symbols on their COMMON dates: prices
        are inner-joined by date first, so a missing day in one series spans two
        days for everyone instead of shifting every earlier return by one."""
        import pandas as pd

        if not price_map:
            return pd.DataFrame()
        prices = pd.DataFrame(price_map).sort_index().dropna(how="any")
        return prices.pct_change().iloc[1:]

    def _get_daily_returns(
        self, symbol: str, start_date: date, end_date: date
    ) -> np.ndarray | None:
        """Single-symbol returns (kept for backward compat; prefer _bulk_daily_returns)."""
        return self._bulk_daily_returns([symbol], start_date, end_date).get(symbol)

    # ── Look-through HHI helpers ────────────────────────────────────────────

    def _get_fund_holdings(
        self, symbols: list[str]
    ) -> dict[str, dict[str, float]]:
        """
        For each symbol that yfinance can expand (ETFs, mutual funds), return a
        mapping {fund_symbol: {underlying_symbol: weight_fraction_of_fund}}.

        weight_fraction_of_fund is in 0..1. Only symbols where yfinance returns
        non-empty top_holdings are included; the rest are silently skipped.
        Results are cached in-process for the life of this service instance to
        avoid redundant network calls when the page is refreshed.
        """
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            return {}

        if not hasattr(self, "_fund_holdings_cache"):
            self._fund_holdings_cache: dict[str, dict[str, float]] = {}

        result: dict[str, dict[str, float]] = {}
        to_fetch = [s for s in symbols if s not in self._fund_holdings_cache]
        # Each expansion is a separate yfinance call; with many funds this is the
        # slowest thing on the risk page. Only reach out in an explicit-refresh
        # context — a normal render uses whatever is already cached and skips the
        # rest (less look-through detail, but no multi-second hang).
        if not network_allowed():
            to_fetch = []

        for sym in to_fetch:
            ysym = yahoo_symbol(sym)
            if ysym is None:   # CUSIP / junk — a fund lookup can only 404
                self._fund_holdings_cache[sym] = {}
                continue
            try:
                fd = yf.Ticker(ysym).funds_data
                th = fd.top_holdings if fd is not None else None
                if th is not None and not th.empty and "Holding Percent" in th.columns:
                    holdings: dict[str, float] = {}
                    for idx, row in th.iterrows():
                        pct = row["Holding Percent"]
                        if pd.notna(pct) and float(pct) > 0:
                            holdings[str(idx)] = float(pct)
                    self._fund_holdings_cache[sym] = holdings
                else:
                    self._fund_holdings_cache[sym] = {}  # no data — cache miss
            except Exception:
                self._fund_holdings_cache[sym] = {}

        for sym in symbols:
            h = self._fund_holdings_cache.get(sym, {})
            if h:
                result[sym] = h

        return result

    def _compute_lookthrough_hhi(
        self,
        holdings: list,  # list of HoldingSummary (have .symbol, .weight_pct, .name)
        fund_holdings_map: dict[str, dict[str, float]],
        lt_ticker_map: dict[str, str] | None = None,
    ) -> tuple[float | None, int | None, float | None, list[LookthroughPosition]]:
        """
        Expand each fund/ETF in `holdings` to its underlying securities using
        `fund_holdings_map`, then compute the look-through HHI.

        lt_ticker_map maps holding_symbol → public lookup ticker for institutional
        fund classes that mirror a public fund (e.g. Contrafund Pool → FCNTX).
        When a holding's own symbol has no fund_holdings_map entry, the lookup
        ticker is tried instead.

        Returns (hhi_lookthrough, num_positions, coverage_pct, top_15_positions).
        coverage_pct = what fraction of portfolio MV was successfully expanded.
        """
        if not holdings:
            return None, None, None, []

        lt_ticker_map = lt_ticker_map or {}

        # Aggregate look-through weights: underlying_sym -> portfolio weight pct
        lt_weights: dict[str, float] = {}
        # Track which fund each underlying came from (first appearance wins for display)
        lt_source: dict[str, str | None] = {}
        expanded_weight = 0.0  # running sum of portfolio weight that was expanded

        for h in holdings:
            sym = h.symbol
            w = float(h.weight_pct)  # already in 0-100 range

            # Resolve which ticker to look up: prefer direct symbol, fall back to alias
            lookup_sym = sym
            display_label = sym  # what to show as the "fund" in ↳ attribution
            if sym and sym not in fund_holdings_map and sym in lt_ticker_map:
                lookup_sym = lt_ticker_map[sym]
                display_label = f"{sym} ({lookup_sym})"
            elif not sym:
                key = f"asset_{h.asset_id}" if hasattr(h, 'asset_id') else None
                if key and key in lt_ticker_map:
                    lookup_sym = lt_ticker_map[key]
                    display_label = h.name[:20] if h.name else key

            if lookup_sym and lookup_sym in fund_holdings_map:
                fh = fund_holdings_map[lookup_sym]  # {underlying: fraction_of_fund}
                covered_fraction = sum(fh.values())

                # Expand each reported constituent
                for underlying, fund_pct in fh.items():
                    portfolio_pct = w * fund_pct
                    if underlying not in lt_weights:
                        lt_source[underlying] = display_label
                    lt_weights[underlying] = lt_weights.get(underlying, 0.0) + portfolio_pct

                # Unexplained remainder stays attributed to the fund shell itself
                remainder_pct = max(0.0, 1.0 - covered_fraction)
                if remainder_pct > 0:
                    remainder_w = w * remainder_pct
                    shell_label = sym or (h.name[:20] if h.name else "fund")
                    if shell_label not in lt_weights:
                        # Mark remainder as via the lookup ticker so the UI shows
                        # "↳ FCNTX (rem.)" rather than "(direct)" for the unexplained portion
                        lt_source[shell_label] = f"{lookup_sym} rem."
                    lt_weights[shell_label] = lt_weights.get(shell_label, 0.0) + remainder_w

                expanded_weight += w * covered_fraction

            else:
                # Direct holding or fund with no holdings data — count as-is
                label = sym if sym else (h.name[:30] if h.name else "private")
                if label not in lt_weights:
                    lt_source[label] = None
                lt_weights[label] = lt_weights.get(label, 0.0) + w

        # Normalise to exactly 100% (floating-point drift correction)
        total = sum(lt_weights.values())
        if total <= 0:
            return None, None, None, []
        lt_weights = {k: v / total * 100.0 for k, v in lt_weights.items()}

        # HHI (DOJ scale: weights in %, so max = 100² = 10,000)
        hhi_lt = sum(w ** 2 for w in lt_weights.values())

        # Coverage %: portfolio weight that was expanded through to constituents
        total_port_weight = sum(float(h.weight_pct) for h in holdings)
        coverage = (expanded_weight / total_port_weight * 100.0) if total_port_weight > 0 else 0.0

        # Top 15 by portfolio weight for display
        top15 = sorted(lt_weights.items(), key=lambda x: -x[1])[:15]
        top_positions = [
            LookthroughPosition(
                symbol=sym,
                weight_pct=Decimal(str(round(w, 2))),
                via_fund=lt_source.get(sym),
            )
            for sym, w in top15
        ]

        return hhi_lt, len(lt_weights), coverage, top_positions

    def _empty_risk_metrics(self, lookback_days: int, risk_free_rate: float) -> RiskMetrics:
        return RiskMetrics(
            portfolio_volatility=Decimal("0"),
            sharpe_ratio=Decimal("0"),
            max_drawdown=Decimal("0"),
            max_drawdown_period="N/A",
            beta=Decimal("0"),
            risk_free_rate=Decimal(str(risk_free_rate)),
            lookback_days=lookback_days,
            position_risks=[],
            concentration=ConcentrationRisk(
                top_5_weight_pct=Decimal("0"), top_10_weight_pct=Decimal("0"),
                largest_position="N/A", largest_position_weight_pct=Decimal("0"),
                hhi_index=Decimal("0"),
            ),
            asset_class_risks=[],
        )

    # ── Investment Profiles ─────────────────────────────────────────────

    def list_profiles(self) -> list[InvestmentProfileSchema]:
        profiles = (
            self.session.query(InvestmentProfile)
            .order_by(InvestmentProfile.is_system.desc(), InvestmentProfile.name)
            .all()
        )
        return [self._profile_to_schema(p) for p in profiles]

    def get_active_profile(self) -> InvestmentProfileSchema | None:
        p = self.session.query(InvestmentProfile).filter(InvestmentProfile.is_active == True).first()
        return self._profile_to_schema(p) if p else None

    def set_active_profile(self, profile_id: int) -> InvestmentProfileSchema:
        # Deactivate all
        self.session.query(InvestmentProfile).update({"is_active": False})
        p = self.session.get(InvestmentProfile, profile_id)
        if not p:
            raise ValueError(f"Profile {profile_id} not found")
        p.is_active = True
        self.session.flush()
        return self._profile_to_schema(p)

    def create_custom_profile(
        self, name: str, target_allocations: dict[str, Decimal], description: str = ""
    ) -> InvestmentProfileSchema:
        total = sum(target_allocations.values())
        if abs(total - 100) > Decimal("0.5"):
            raise ValueError(f"Target allocations must sum to 100%, got {total}%")

        p = InvestmentProfile(
            name=name,
            risk_tolerance=RiskToleranceEnum.CUSTOM,
            is_system=False,
            is_active=False,
            description=description,
            target_allocations_json=json.dumps({k: str(v) for k, v in target_allocations.items()}),
        )
        self.session.add(p)
        self.session.flush()
        return self._profile_to_schema(p)

    def delete_profile(self, profile_id: int) -> bool:
        p = self.session.get(InvestmentProfile, profile_id)
        if not p or p.is_system:
            return False
        self.session.delete(p)
        self.session.flush()
        return True

    def _profile_to_schema(self, p: InvestmentProfile) -> InvestmentProfileSchema:
        allocs = json.loads(p.target_allocations_json)
        rt = p.risk_tolerance
        rt_str = rt.value if hasattr(rt, "value") else str(rt)
        return InvestmentProfileSchema(
            id=p.id,
            name=p.name,
            risk_tolerance=rt_str,
            is_system=p.is_system,
            is_active=p.is_active,
            is_comparison_a=bool(getattr(p, "is_comparison_a", False)),
            is_comparison_b=bool(getattr(p, "is_comparison_b", False)),
            description=p.description,
            target_allocations={k: Decimal(v) for k, v in allocs.items()},
        )

    def get_comparison_profiles(
        self,
    ) -> tuple["InvestmentProfileSchema | None", "InvestmentProfileSchema | None"]:
        """Return the profiles selected as Comparison A and B (either may be None)."""
        a = self.session.query(InvestmentProfile).filter(
            InvestmentProfile.is_comparison_a == True
        ).first()
        b = self.session.query(InvestmentProfile).filter(
            InvestmentProfile.is_comparison_b == True
        ).first()
        return (
            self._profile_to_schema(a) if a else None,
            self._profile_to_schema(b) if b else None,
        )

    def set_comparison_profile(self, profile_id: int, slot: str) -> None:
        """Set a profile as Comparison A or B (slot = 'a' or 'b').
        Clears any previous selection for that slot first.
        """
        if slot == "a":
            self.session.query(InvestmentProfile).update({"is_comparison_a": False})
            p = self.session.get(InvestmentProfile, profile_id)
            if p:
                p.is_comparison_a = True
        else:
            self.session.query(InvestmentProfile).update({"is_comparison_b": False})
            p = self.session.get(InvestmentProfile, profile_id)
            if p:
                p.is_comparison_b = True
        self.session.flush()

    # ── Drift & Rebalancing ─────────────────────────────────────────────

    def get_drift_analysis(self, profile_id: int | None = None) -> DriftAnalysis:
        """Compare current allocation vs target and generate rebalance actions."""
        if profile_id:
            p = self.session.get(InvestmentProfile, profile_id)
            if not p:
                raise ValueError(f"Profile {profile_id} not found")
            profile = self._profile_to_schema(p)
        else:
            profile = self.get_active_profile()
            if not profile:
                raise ValueError("No active investment profile. Please activate a profile first.")

        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        current_alloc = {k: float(v) for k, v in summary.allocation.items()}

        targets = []
        rebalance_actions = []
        total_rebalance = Decimal("0")

        for ac, target_pct in profile.target_allocations.items():
            t_pct = float(target_pct)
            c_pct = current_alloc.get(ac, 0.0)
            drift = round(c_pct - t_pct, 2)

            if abs(drift) < 1.0:
                status = "on_target"
            elif drift > 0:
                status = "overweight"
            else:
                status = "underweight"

            targets.append(AllocationTarget(
                asset_class=ac,
                target_pct=Decimal(str(t_pct)),
                current_pct=Decimal(str(round(c_pct, 2))),
                drift_pct=Decimal(str(drift)),
                drift_status=status,
            ))

            if abs(drift) >= 1.0 and total_mv > 0:
                dollar_amount = abs(drift) / 100 * total_mv
                action = "sell" if drift > 0 else "buy"

                # Find the largest holding in this asset class for suggestion
                suggested = None
                for h in summary.holdings:
                    if h.asset_class == ac and h.symbol:
                        suggested = h.symbol
                        break

                rebalance_actions.append(RebalanceAction(
                    asset_class=ac,
                    action=action,
                    target_change_pct=Decimal(str(abs(drift))),
                    estimated_dollar_amount=Decimal(str(round(dollar_amount, 2))),
                    suggested_symbol=suggested,
                ))
                total_rebalance += Decimal(str(round(dollar_amount, 2)))

        # Add current allocations not in target
        for ac, c_pct in current_alloc.items():
            if ac not in profile.target_allocations and c_pct > 0:
                targets.append(AllocationTarget(
                    asset_class=ac,
                    target_pct=Decimal("0"),
                    current_pct=Decimal(str(round(c_pct, 2))),
                    drift_pct=Decimal(str(round(c_pct, 2))),
                    drift_status="overweight",
                ))
                if total_mv > 0:
                    dollar_amount = c_pct / 100 * total_mv
                    suggested = None
                    for h in summary.holdings:
                        if h.asset_class == ac and h.symbol:
                            suggested = h.symbol
                            break
                    rebalance_actions.append(RebalanceAction(
                        asset_class=ac, action="sell",
                        target_change_pct=Decimal(str(round(c_pct, 2))),
                        estimated_dollar_amount=Decimal(str(round(dollar_amount, 2))),
                        suggested_symbol=suggested,
                    ))
                    total_rebalance += Decimal(str(round(dollar_amount, 2)))

        return DriftAnalysis(
            profile_name=profile.name,
            risk_tolerance=profile.risk_tolerance,
            targets=sorted(targets, key=lambda t: abs(t.drift_pct), reverse=True),
            rebalance_actions=sorted(rebalance_actions, key=lambda a: a.estimated_dollar_amount, reverse=True),
            total_rebalance_value=total_rebalance,
        )

    # ── Monte Carlo Simulation ──────────────────────────────────────────

    def _resolve_portfolio_regime(self, summary, initial_value, regime_override):
        """Manual override → synthetic regime; else auto Markov (never blocks)."""
        if regime_override and regime_override.lower() != "base":
            from app.schemas.analysis import RegimeTransition
            _rt = RegimeTransition(bull=0.0, sideways=0.0, bear=0.0)
            return RegimeState(
                symbol="Manual Override", current_regime=regime_override.capitalize(),
                persist_pct=0.0, one_step=_rt, five_step=_rt, stationary=_rt,
                signal_score=0.0, lookback_days=0,
            )
        if not regime_override:
            try:
                from app.services.markov_regime_service import MarkovRegimeService
                from app.services.market_data import stored_price_map
                symbols = [h.symbol for h in summary.holdings if h.symbol]
                # Holdings are per (asset, account): sum a symbol held in several
                # accounts instead of letting the last account overwrite the rest.
                weights: dict[str, float] = {}
                for h in summary.holdings:
                    if h.symbol and float(h.market_value) > 0:
                        weights[h.symbol] = weights.get(h.symbol, 0.0) + float(h.market_value) / initial_value
                if symbols:
                    # Stored history only — same source the morning brief uses.
                    # This used to be a live 2-year yf.download of every raw
                    # holding symbol on each Monte Carlo render, which hammered
                    # Yahoo (rate limits → "possibly delisted" for the whole
                    # portfolio) and errored per CUSIP/BRK-slash symbol. The
                    # backfill keeps this history fresh; if it's still thin the
                    # regime is simply None → unconditioned simulation.
                    price_map = stored_price_map(self.session, symbols)
                    regime, _ = MarkovRegimeService().portfolio_and_ticker_regimes_from_prices(
                        price_map=price_map, weights=weights
                    )
                    return regime
            except Exception:
                pass
        return None  # "base" or detection unavailable → no conditioning

    def solve_max_spending(
        self,
        *,
        years: int = 10,
        num_simulations: int = 2000,
        monthly_contribution: Decimal = Decimal("0"),
        withdrawal_years: int = 30,
        regime_override: str | None = None,
        spending_pattern: str = "constant",
        smile_slow_pct: float = 80.0,
        smile_no_pct: float = 65.0,
        current_age: int | None = None,
        retirement_age: int | None = None,
        ssa_claiming_age: int | None = None,
        ssa_monthly_fra: Decimal = Decimal("0"),
        fra_age: int = 67,
        spouse_monthly_fra: Decimal = Decimal("0"),
        spouse_claiming_age: int | None = None,
        spouse_current_age: int | None = None,
        spouse_fra_age: int = 67,
        income_bridge: bool = False,
        bucket_balances: dict[str, float] | None = None,
        deferred_tax_rate: float = 0.18,
        taxable_tax_rate: float = 0.10,
        inflation_rate: float = 0.0,
        budget_items: list[dict] | None = None,
        reinvest_surplus: bool = False,
        target_survival: float = 85.0,
        max_rate: float = 20.0,
    ) -> "MaxSpendingResult":
        """Find the highest withdrawal rate keeping portfolio survival ≥ target.

        Uses exactly the displayed run's scenario (_mc_scenario: ages, household
        SSA, income bridge, taxes, buckets). With a fixed-dollar withdrawal and
        common random numbers, survival falls as spending rises, so a binary
        search finds the crossing point. In budget mode it searches a scale
        factor on the whole budget (same fixed/variable mix) instead of a rate.
        """
        from app.schemas.analysis import MaxSpendingResult

        sc = self._mc_scenario(
            years=years, withdrawal_years=withdrawal_years, regime_override=regime_override,
            current_age=current_age, retirement_age=retirement_age,
            ssa_claiming_age=ssa_claiming_age, ssa_monthly_fra=ssa_monthly_fra, fra_age=fra_age,
            spouse_monthly_fra=spouse_monthly_fra, spouse_claiming_age=spouse_claiming_age,
            spouse_current_age=spouse_current_age, spouse_fra_age=spouse_fra_age,
            income_bridge=income_bridge, bucket_balances=bucket_balances,
            deferred_tax_rate=deferred_tax_rate, taxable_tax_rate=taxable_tax_rate,
        )
        wy = int(withdrawal_years)
        target = float(target_survival)
        spend_kw = _spending_kwargs(inflation_rate, budget_items, reinvest_surplus)
        budget_mode = "spending_fixed_annual" in spend_kw
        if budget_mode:
            # Search the budget scale k in [0, max_scale]; "rate" below means k.
            budget_annual = spend_kw["spending_fixed_annual"] + spend_kw["spending_variable_annual"]
            max_rate = 10.0

        def _sim_at(rate: float):
            return self._simulate(
                label="solve", allocation=sc["allocation"], initial_value=sc["initial_value"],
                years=sc["years"], num_simulations=num_simulations, goal_amount=None,
                regime=sc["regime"], monthly_contribution=float(monthly_contribution),
                withdrawal_rate=0.0 if budget_mode else rate, withdrawal_years=wy,
                spending_pattern=spending_pattern,
                smile_slow_pct=smile_slow_pct, smile_no_pct=smile_no_pct,
                current_age=sc["current_age"], retirement_age=sc["retirement_age"],
                ssa_claiming_age=sc["ssa_claiming_age"], ssa_fra_factor=sc["ssa_fra_factor"],
                **({**spend_kw, "spending_scale": rate} if budget_mode else spend_kw),
                **sc["shared_kwargs"],
            )

        def _survival(rate: float) -> float:
            r = _sim_at(rate)
            return float(r.portfolio_survival_rate or 0.0)

        # Binary search for the highest rate whose survival is still ≥ target.
        # The lowest rate tried (~0) must meet the target, else it's unreachable.
        reachable = True
        if _survival(max_rate) >= target:
            best = max_rate
        else:
            lo, hi = 0.0, max_rate
            for _ in range(20):
                mid = (lo + hi) / 2.0
                if _survival(mid) >= target:
                    lo = mid
                else:
                    hi = mid
            best = lo
            reachable = _survival(best) >= target

        r = _sim_at(best)
        annual = r.withdrawal_annual_amount or Decimal("0")
        budget_fields = {}
        if budget_mode:
            budget_fields = dict(
                spending_mode="budget",
                budget_monthly=Decimal(str(round(budget_annual / 12, 2))),
                budget_pct_of_max=(Decimal(str(round(100 / best, 1))) if best > 0 else None),
            )
        return MaxSpendingResult(
            **budget_fields,
            inflation_rate=Decimal(str(round(spend_kw["inflation_rate"] * 100, 2))),
            target_survival=Decimal(str(round(target, 1))),
            max_withdrawal_rate=(r.implied_withdrawal_rate or Decimal("0")) if budget_mode
                                else Decimal(str(round(best, 2))),
            annual_amount=annual,
            monthly_amount=Decimal(str(round(float(annual) / 12.0, 2))),
            achieved_survival=r.portfolio_survival_rate or Decimal("0"),
            ssa_annual_benefit=r.ssa_annual_benefit,
            withdrawal_years=wy,
            target_reachable=reachable,
            income_bridge=bool(r.income_bridge),
        )

    def _mc_scenario(
        self,
        *,
        years: int,
        withdrawal_years: int,
        regime_override: str | None,
        current_age: int | None,
        retirement_age: int | None,
        ssa_claiming_age: int | None,
        ssa_monthly_fra: Decimal,
        fra_age: int,
        spouse_monthly_fra: Decimal,
        spouse_claiming_age: int | None,
        spouse_current_age: int | None,
        spouse_fra_age: int,
        income_bridge: bool,
        bucket_balances: dict[str, float] | None,
        deferred_tax_rate: float,
        taxable_tax_rate: float,
    ) -> dict:
        """Everything a Monte Carlo run needs besides the allocation and the
        withdrawal rate: starting value, regime, timeline, household SSA
        schedule and income-bridge buckets. Shared by run_monte_carlo and
        solve_max_spending so the solver always tests the displayed scenario.
        """
        summary = self.portfolio_svc.get_summary()
        initial_value = float(summary.total_market_value)
        if initial_value <= 0:
            initial_value = 1.0

        portfolio_regime = self._resolve_portfolio_regime(summary, initial_value, regime_override)

        # Current allocation weights
        current_alloc = {k: float(v) / 100 for k, v in summary.allocation.items()}

        withdrawal_years_int   = int(withdrawal_years)

        # ── Retirement timeline & Social Security ─────────────────────────────
        # When current + retirement ages are supplied, they drive the
        # accumulation length (years to retirement). Ages are the friendly
        # overlay; the engine still thinks in "years from today".
        if current_age is not None and retirement_age is not None:
            years = max(0, int(retirement_age) - int(current_age))

        # ── Social Security (one or two earners) ──────────────────────────────
        # Build the per-year household benefit schedule. SSA only participates
        # when we know *when* it starts (ages) and how much (a positive benefit);
        # otherwise the schedule is all-zero and the model is identical to before.
        # ssa_fra_factor / the single ssa_* result fields describe the PRIMARY.
        ssa_fra_factor: float | None = None
        if (ssa_monthly_fra and float(ssa_monthly_fra) > 0
                and ssa_claiming_age is not None and retirement_age is not None):
            ssa_fra_factor = self._ssa_claiming_factor(int(ssa_claiming_age), int(fra_age))

        ssa_schedule, ssa_household = self._household_ssa(
            withdrawal_years=withdrawal_years_int,
            accumulation_years=years,
            retirement_age=int(retirement_age) if retirement_age is not None else None,
            primary_monthly_fra=float(ssa_monthly_fra),
            primary_claiming_age=int(ssa_claiming_age) if ssa_claiming_age is not None else None,
            primary_fra_age=int(fra_age),
            spouse_monthly_fra=float(spouse_monthly_fra),
            spouse_claiming_age=int(spouse_claiming_age) if spouse_claiming_age is not None else None,
            spouse_current_age=int(spouse_current_age) if spouse_current_age is not None else None,
            spouse_fra_age=int(spouse_fra_age),
        )

        ca = int(current_age) if current_age is not None else None
        ra = int(retirement_age) if retirement_age is not None else None
        sca = int(ssa_claiming_age) if ssa_claiming_age is not None else None

        # ── Income Bridge: account-type buckets for withdrawal sequencing ──────
        # When enabled, derive today's taxable/traditional/Roth balances from the
        # real accounts unless explicit balances were supplied (manual override).
        if income_bridge and bucket_balances is None:
            bucket_balances = {
                k: float(v) for k, v in self.portfolio_svc.get_tax_bucket_balances().items()
            }
        # The bridge models the portfolio AS the sum of its buckets. For the
        # auto-derived path that already equals total market value; for manual
        # overrides it lets entered balances drive the scale (e.g. before the
        # real accounts are loaded) instead of an empty ledger's $0.
        if income_bridge and bucket_balances:
            bucket_total = sum(max(0.0, float(v)) for v in bucket_balances.values())
            if bucket_total > 0:
                initial_value = bucket_total
        shared_kwargs = dict(
            ssa_schedule=ssa_schedule,
            ssa_household=ssa_household,
            income_bridge=income_bridge,
            bucket_balances=bucket_balances,
            deferred_tax_rate=deferred_tax_rate,
            taxable_tax_rate=taxable_tax_rate,
        )
        return dict(
            initial_value=initial_value, regime=portfolio_regime, allocation=current_alloc,
            years=years, current_age=ca, retirement_age=ra, ssa_claiming_age=sca,
            ssa_fra_factor=ssa_fra_factor, shared_kwargs=shared_kwargs,
        )

    _FIXED_HINTS = ("mortgage", "rent", "housing", "property tax", "tax", "insurance", "utilit",
                    "electric", "water", "gas", "internet", "phone", "health", "medical",
                    "premium", "hoa", "loan", "car payment", "childcare", "tuition", "subscription")

    def expense_baseline(self, months: int = 12) -> list[dict]:
        """Average monthly spending per expense category over the last ``months``
        months, as budget items for the Monte Carlo budget mode. A category is
        marked fixed when most of its spending is recurring or its name looks
        like a fixed cost (housing, insurance, utilities, …); edit as needed."""
        from app.models.expense import Expense, ExpenseCategory

        since = date.today() - timedelta(days=round(months * 30.44))
        rows = (
            self.session.query(ExpenseCategory.name, Expense.amount, Expense.is_recurring)
            .join(Expense, Expense.category_id == ExpenseCategory.id)
            .filter(Expense.expense_date >= since)
            .all()
        )
        totals: dict[str, list[float]] = {}
        for name, amount, recurring in rows:
            t = totals.setdefault(name, [0.0, 0.0])
            t[0] += float(amount or 0)
            if recurring:
                t[1] += float(amount or 0)
        items = []
        for name, (total, recurring_total) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
            if total <= 0:
                continue
            lname = name.lower()
            fixed = recurring_total >= total / 2 or any(h in lname for h in self._FIXED_HINTS)
            items.append({"name": name, "monthly": round(total / months, 2), "fixed": fixed})
        return items

    def run_monte_carlo(
        self,
        years: int = 10,
        num_simulations: int = 1000,
        goal_amount: Decimal | None = None,
        include_target: bool = True,
        monthly_contribution: Decimal = Decimal("0"),
        withdrawal_rate: Decimal = Decimal("0"),
        withdrawal_years: int = 0,
        regime_override: str | None = None,  # "bull" | "sideways" | "bear" | "base" | None=auto
        spending_pattern: str = "constant",  # "constant" | "smile"
        smile_slow_pct: float = 80.0,
        smile_no_pct: float = 65.0,
        current_age: int | None = None,
        retirement_age: int | None = None,
        ssa_claiming_age: int | None = None,
        ssa_monthly_fra: Decimal = Decimal("0"),  # est. monthly benefit at Full Retirement Age (67)
        fra_age: int = 67,                                  # primary's full retirement age (66–67)
        spouse_monthly_fra: Decimal = Decimal("0"),         # spouse benefit at their FRA ($/mo)
        spouse_claiming_age: int | None = None,             # spouse claims at this age (62–70)
        spouse_current_age: int | None = None,              # spouse's age today (places them on the timeline)
        spouse_fra_age: int = 67,                           # spouse's full retirement age (66–67)
        income_bridge: bool = False,                       # model account-type withdrawal sequencing + taxes
        bucket_balances: dict[str, float] | None = None,   # taxable/traditional/roth $; auto-derived if None
        deferred_tax_rate: float = 0.18,                   # effective tax on tax-deferred withdrawals (fraction)
        taxable_tax_rate: float = 0.10,                    # effective cap-gains drag on taxable withdrawals (fraction)
        inflation_rate: float = 0.0,                       # annual inflation (fraction); results in today's $
        budget_items: list[dict] | None = None,            # itemised monthly spending → budget mode
        reinvest_surplus: bool = False,                    # income above spending goes back into the portfolio
    ) -> MonteCarloComparison:
        """
        Run Monte Carlo simulation for current allocation and optionally target.

        Uses geometric Brownian motion with annual steps. Per-asset-class return
        and volatility are from historical data or ASSET_CLASS_ASSUMPTIONS.

        Applies Markov regime conditioning to the first 2 years of the simulation:
          Bull  regime → +2% expected return boost, −10% volatility (year 1: full, year 2: half)
          Bear  regime → −3% return drag,           +20% volatility
          Sideways     → base assumptions
        """
        sc = self._mc_scenario(
            years=years, withdrawal_years=withdrawal_years, regime_override=regime_override,
            current_age=current_age, retirement_age=retirement_age,
            ssa_claiming_age=ssa_claiming_age, ssa_monthly_fra=ssa_monthly_fra, fra_age=fra_age,
            spouse_monthly_fra=spouse_monthly_fra, spouse_claiming_age=spouse_claiming_age,
            spouse_current_age=spouse_current_age, spouse_fra_age=spouse_fra_age,
            income_bridge=income_bridge, bucket_balances=bucket_balances,
            deferred_tax_rate=deferred_tax_rate, taxable_tax_rate=taxable_tax_rate,
        )
        initial_value = sc["initial_value"]
        portfolio_regime = sc["regime"]
        current_alloc = sc["allocation"]
        years = sc["years"]
        ca, ra, sca = sc["current_age"], sc["retirement_age"], sc["ssa_claiming_age"]
        ssa_fra_factor = sc["ssa_fra_factor"]
        shared_kwargs = {**sc["shared_kwargs"],
                         **_spending_kwargs(inflation_rate, budget_items, reinvest_surplus)}
        monthly_contrib_float  = float(monthly_contribution)
        withdrawal_rate_float  = float(withdrawal_rate)
        withdrawal_years_int   = int(withdrawal_years)

        # Run simulation for current allocation
        current_result = self._simulate(
            label="Current Allocation",
            allocation=current_alloc,
            initial_value=initial_value,
            years=years,
            num_simulations=num_simulations,
            goal_amount=float(goal_amount) if goal_amount else None,
            regime=portfolio_regime,
            monthly_contribution=monthly_contrib_float,
            withdrawal_rate=withdrawal_rate_float,
            withdrawal_years=withdrawal_years_int,
            spending_pattern=spending_pattern,
            smile_slow_pct=smile_slow_pct,
            smile_no_pct=smile_no_pct,
            current_age=ca,
            retirement_age=ra,
            ssa_claiming_age=sca,
            ssa_fra_factor=ssa_fra_factor,
            **shared_kwargs,
        )

        # Run simulation for target allocation if profile is active
        target_result = None
        if include_target:
            profile = self.get_active_profile()
            if profile:
                target_alloc = {k: float(v) / 100 for k, v in profile.target_allocations.items()}
                target_result = self._simulate(
                    label=f"Target: {profile.name}",
                    allocation=target_alloc,
                    initial_value=initial_value,
                    years=years,
                    num_simulations=num_simulations,
                    goal_amount=float(goal_amount) if goal_amount else None,
                    regime=portfolio_regime,
                    monthly_contribution=monthly_contrib_float,
                    withdrawal_rate=withdrawal_rate_float,
                    withdrawal_years=withdrawal_years_int,
                    spending_pattern=spending_pattern,
                    smile_slow_pct=smile_slow_pct,
                    smile_no_pct=smile_no_pct,
                    current_age=ca,
                    retirement_age=ra,
                    ssa_claiming_age=sca,
                    ssa_fra_factor=ssa_fra_factor,
                    **shared_kwargs,
                )

        # ── Comparison A / B simulations (user-selected profiles) ────────────
        def _sim_from_profile(profile: "InvestmentProfileSchema | None") -> "MonteCarloResult | None":
            if not profile:
                return None
            alloc = {k: float(v) / 100 for k, v in profile.target_allocations.items() if float(v) > 0}
            return self._simulate(
                label=profile.name,
                allocation=alloc,
                initial_value=initial_value,
                years=years,
                num_simulations=num_simulations,
                goal_amount=float(goal_amount) if goal_amount else None,
                regime=portfolio_regime,
                monthly_contribution=monthly_contrib_float,
                withdrawal_rate=withdrawal_rate_float,
                withdrawal_years=withdrawal_years_int,
                spending_pattern=spending_pattern,
                smile_slow_pct=smile_slow_pct,
                smile_no_pct=smile_no_pct,
                current_age=ca,
                retirement_age=ra,
                ssa_claiming_age=sca,
                ssa_fra_factor=ssa_fra_factor,
                **shared_kwargs,
            )

        profile_a, profile_b = self.get_comparison_profiles()
        comparison_a_result  = _sim_from_profile(profile_a)
        comparison_b_result  = _sim_from_profile(profile_b)

        return MonteCarloComparison(
            current=current_result,
            target=target_result,
            comparison_a=comparison_a_result,
            comparison_b=comparison_b_result,
            goal_amount=goal_amount,
        )

    @staticmethod
    def _ssa_claiming_factor(claiming_age: int, fra: int = 67) -> float:
        """Benefit as a fraction of the Full-Retirement-Age (FRA) amount.

        Applies the standard Social Security rules (FRA = 67 for those born 1960+):
          • Claim early (before FRA): permanent reduction of 5/9 of 1% per month
            for the first 36 months, then 5/12 of 1% per month beyond that.
            → age 62 yields 70% of the FRA benefit.
          • Claim late (after FRA): delayed-retirement credits of 8%/year up to
            age 70. → age 70 yields 124% of the FRA benefit.
        Claiming age is clamped to the valid [62, 70] window.
        """
        age = max(62, min(70, int(claiming_age)))
        if age == fra:
            return 1.0
        if age < fra:
            early_months = (fra - age) * 12
            first36 = min(early_months, 36)
            beyond = max(early_months - 36, 0)
            reduction = first36 * (5.0 / 9.0 / 100.0) + beyond * (5.0 / 12.0 / 100.0)
            return round(1.0 - reduction, 6)
        # Delayed credits accrue only through age 70.
        delayed_years = min(age, 70) - fra
        return round(1.0 + delayed_years * 0.08, 6)

    @staticmethod
    def _ssa_spousal_factor(claiming_age: int, fra: int = 67) -> float:
        """Spousal benefit as a fraction of the 50%-of-worker's-FRA amount.

        Spousal benefits are reduced when claimed before the claimant's FRA
        (25/36 of 1%/month for the first 36 months, then 5/12 of 1%/month) and,
        unlike worker benefits, earn NO delayed credits past FRA:
          • at 62 (FRA 67) → ~65% of the 50% spousal amount (≈32.5% of the PIA).
          • at/after FRA   → the full 50%.
        """
        age = max(62, min(int(fra), int(claiming_age)))  # capped at FRA — no delayed credit
        if age >= fra:
            return 1.0
        early_months = (fra - age) * 12
        first36 = min(early_months, 36)
        beyond = max(early_months - 36, 0)
        reduction = first36 * (25.0 / 36.0 / 100.0) + beyond * (5.0 / 12.0 / 100.0)
        return round(1.0 - reduction, 6)

    def _household_ssa(
        self,
        *,
        withdrawal_years: int,
        accumulation_years: int,
        retirement_age: int | None,
        primary_monthly_fra: float,
        primary_claiming_age: int | None,
        primary_fra_age: int = 67,
        spouse_monthly_fra: float = 0.0,
        spouse_claiming_age: int | None = None,
        spouse_current_age: int | None = None,
        spouse_fra_age: int = 67,
    ) -> "tuple[list[float], SsaHousehold | None]":
        """Per-year household Social Security schedule + a two-earner summary.

        Models up to two earners on their OWN age timelines: each claims at
        their own age (62–70) with the standard reduction/delayed-credit factor;
        the lower earner gets a spousal top-up toward 50% of the higher earner's
        FRA benefit (reduced if claimed early, payable only once the higher
        earner has also claimed); the survivor gets the deceased higher earner's
        benefit, floored at 82.5% of their PIA (the widow(er)'s limit).

        Returns (annual $ per distribution year, summary). The summary is None
        when there's no spouse, so the single-stream behavior is unchanged. A
        spouse counts once their current and claiming ages are given, even with
        no benefit of their own ($0): they can still collect the spousal benefit.
        """
        def _dec(x) -> Decimal:
            return Decimal(str(round(float(x), 2)))

        primary_on = (primary_monthly_fra > 0 and primary_claiming_age is not None
                      and retirement_age is not None and withdrawal_years > 0)
        spouse_on = (spouse_monthly_fra >= 0 and spouse_claiming_age is not None
                     and spouse_current_age is not None and retirement_age is not None
                     and withdrawal_years > 0)
        if not primary_on and not (spouse_on and spouse_monthly_fra > 0):
            return [0.0] * withdrawal_years, None

        def _person(monthly_fra, claiming_age, fra_age, age_at):
            factor = self._ssa_claiming_factor(int(claiming_age), int(fra_age))
            active = [age_at(y) >= int(claiming_age) for y in range(withdrawal_years)]
            return {
                "monthly_fra": float(monthly_fra), "claiming_age": int(claiming_age),
                "fra_age": int(fra_age), "factor": factor,
                "own_monthly": float(monthly_fra) * factor,
                "active": active,
                "start": next((y for y, a in enumerate(active) if a), None),
                "age_at": age_at,
            }

        pri = (_person(primary_monthly_fra, primary_claiming_age, primary_fra_age,
                       lambda y: retirement_age + y) if primary_on else None)
        spo = (_person(spouse_monthly_fra, spouse_claiming_age, spouse_fra_age,
                       lambda y: spouse_current_age + accumulation_years + y) if spouse_on else None)

        # Spousal top-up for the lower-FRA earner (only when both participate).
        # SSA rule: the "spousal excess" is 50% of the higher earner's PIA minus
        # the lower earner's own PIA (both unreduced), and only that excess gets
        # the spousal reduction — based on the lower earner's age when the
        # top-up actually starts (i.e. once the higher earner has also filed).
        # E.g. PIAs 4,100/1,900, spouse at 62 (FRA 67): (2,050 − 1,900) × 65% = 97.50.
        topup_monthly, topup_target = 0.0, None
        if pri and spo:
            higher, lower, topup_target = (
                (pri, spo, "spouse") if pri["monthly_fra"] >= spo["monthly_fra"]
                else (spo, pri, "primary"))
            excess = max(0.0, 0.5 * higher["monthly_fra"] - lower["monthly_fra"])
            both_start = next((y for y in range(withdrawal_years)
                               if higher["active"][y] and lower["active"][y]), None)
            topup_age = (max(lower["claiming_age"], lower["age_at"](both_start))
                         if both_start is not None else lower["claiming_age"])
            topup_monthly = excess * self._ssa_spousal_factor(topup_age, lower["fra_age"])

        # Per-year household annual benefit.
        ssa_per_year = [0.0] * withdrawal_years
        for y in range(withdrawal_years):
            monthly = 0.0
            pri_active = pri["active"][y] if pri else False
            spo_active = spo["active"][y] if spo else False
            if pri_active:
                monthly += pri["own_monthly"]
            if spo_active:
                monthly += spo["own_monthly"]
            if topup_target and pri_active and spo_active:  # worker must also have claimed
                monthly += topup_monthly
            ssa_per_year[y] = monthly * 12.0

        def _to_person(p, label, is_topup_target):
            tu = topup_monthly if is_topup_target else 0.0
            total_monthly = p["own_monthly"] + tu
            return SsaPerson(
                label=label, monthly_fra=_dec(p["monthly_fra"]), fra_age=p["fra_age"],
                claiming_age=p["claiming_age"], fra_factor=p["factor"],
                own_monthly_benefit=_dec(p["own_monthly"]), spousal_monthly_benefit=_dec(tu),
                monthly_benefit=_dec(total_monthly), annual_benefit=_dec(total_monthly * 12.0),
                starts_plan_year=(p["start"] + 1) if p["start"] is not None else None,
                starts_age=p["age_at"](p["start"]) if p["start"] is not None else None,
            )

        household = None
        if spo:
            people = ([_to_person(pri, "You", topup_target == "primary")] if pri else []) + [
                _to_person(spo, "Spouse", topup_target == "spouse")]
            if pri:
                # Survivor (higher earner dies first, survivor at their own FRA):
                # the deceased's actual benefit, but never less than 82.5% of their
                # PIA when they had claimed early (the widow(er)'s limit); the
                # survivor keeps their own benefit instead if that's larger.
                higher = pri if pri["monthly_fra"] >= spo["monthly_fra"] else spo
                lower = spo if higher is pri else pri
                widow_monthly = (max(higher["own_monthly"], 0.825 * higher["monthly_fra"])
                                 if higher["factor"] < 1.0 else higher["own_monthly"])
                survivor_monthly = max(widow_monthly, lower["own_monthly"])
            else:
                survivor_monthly = spo["own_monthly"]   # only the spouse has a record
            first = next((y for y in range(withdrawal_years) if ssa_per_year[y] > 0), None)
            # Runs of equal household income, so staggered claim dates are visible
            # (e.g. one check for two years before the second starts).
            phases: list[SsaPhase] = []
            for y in range(first if first is not None else withdrawal_years, withdrawal_years):
                if phases and float(phases[-1].annual_benefit) == round(ssa_per_year[y], 2):
                    phases[-1].to_plan_year = y + 1
                else:
                    phases.append(SsaPhase(from_plan_year=y + 1, to_plan_year=y + 1,
                                           annual_benefit=_dec(ssa_per_year[y])))
            household = SsaHousehold(
                people=people,
                combined_annual_benefit=_dec(max(ssa_per_year) if withdrawal_years else 0.0),
                survivor_annual_benefit=_dec(survivor_monthly * 12.0),
                first_benefit_plan_year=(first + 1) if first is not None else None,
                phases=phases,
            )
        return ssa_per_year, household

    def _run_income_bridge(
        self,
        *,
        final_vals,                       # (num_sims,) accumulation-ending values
        spending_by_year,                 # (withdrawal_years,) after-tax spending need, nominal $
        ssa_per_year,                     # (withdrawal_years,) guaranteed income per year, nominal $
        dist_gf,                          # (num_sims, withdrawal_years) growth factors
        bucket_balances: dict[str, float],
        deferred_tax_rate: float,
        taxable_tax_rate: float,
        withdrawal_years: int,
        num_simulations: int,
        retirement_age: int | None,
        ssa_claiming_age: int | None,
        price_by_year=None,               # (withdrawal_years,) price level per year (reporting in today's $)
        end_price: float = 1.0,           # price level at the end of the plan
        reinvest_surplus: bool = False,   # income above spending is added to the taxable bucket
    ) -> "tuple[np.ndarray, IncomeBridge, np.ndarray]":
        """Distribution phase with account-type buckets and ordered, taxed
        withdrawals — taxable → traditional → Roth.

        The smile-adjusted withdrawal is treated as the household's *after-tax*
        spending need. Each year, guaranteed income (Social Security) is applied
        first; the remaining net need is then pulled from the buckets in order,
        grossing up tax-deferred withdrawals (and lightly taxing taxable ones) so
        the gross drawn reflects the account tapped. Roth is tax-free. A bucket
        that empties is skipped; if every bucket empties the household can't fund
        that year and the portfolio is at ruin.

        Returns the per-year total-portfolio values (same shape the single-pooled
        path produces, so the fan chart and summary stats are unchanged), an
        IncomeBridge summary of medians answering "which accounts fund the
        pre-SSA years", and each simulation's total net (after-tax) draw. All
        reported dollar figures are in today's dollars (``price_by_year``).
        """
        def _d(x) -> Decimal:
            return Decimal(str(round(float(x), 2)))

        order = ("taxable", "traditional", "roth")
        labels = {"taxable": "Taxable",
                  "traditional": "Tax-Deferred",
                  "roth": "Tax-Free (Roth)"}
        rates = {
            "taxable": max(0.0, min(0.95, float(taxable_tax_rate))),
            "traditional": max(0.0, min(0.95, float(deferred_tax_rate))),
            "roth": 0.0,
        }

        # Split each simulation's accumulation-ending value into buckets using
        # today's proportions (accumulation grows/contributes pro-rata).
        start_total = sum(max(0.0, float(bucket_balances.get(b, 0.0))) for b in order)
        props = {b: (max(0.0, float(bucket_balances.get(b, 0.0))) / start_total
                     if start_total > 0 else 0.0)
                 for b in order}
        buckets = {b: final_vals * props[b] for b in order}

        price = (np.asarray(price_by_year, dtype=float) if price_by_year is not None
                 else np.ones(withdrawal_years))
        start_price = float(price[0]) if withdrawal_years else 1.0
        dist_values = np.empty((num_simulations, withdrawal_years), dtype=float)
        taxes_per_sim = np.zeros(num_simulations, dtype=float)
        net_drawn_per_sim = np.zeros(num_simulations, dtype=float)
        gross_by_year = np.zeros((num_simulations, withdrawal_years), dtype=float)
        funding_by_year: list[BridgeYearFunding] = []
        depletes_year: dict[str, int | None] = {b: None for b in order}

        for y in range(withdrawal_years):
            # Grow every bucket, then meet the year's net spending need in order.
            for b in order:
                buckets[b] = buckets[b] * dist_gf[:, y]

            spending_need = float(spending_by_year[y])                       # after-tax $ wanted
            need = np.full(num_simulations, max(spending_need - ssa_per_year[y], 0.0))  # left after SSA

            gross_by_src: dict[str, "np.ndarray"] = {}
            for b in order:
                net_factor = 1.0 - rates[b]
                gross_needed = need / net_factor if net_factor > 0 else need
                gross_taken = np.minimum(buckets[b], gross_needed)
                net_delivered = gross_taken * net_factor
                buckets[b] = buckets[b] - gross_taken
                taxes_per_sim += (gross_taken - net_delivered) / price[y]
                need = need - net_delivered
                net_drawn_per_sim += net_delivered / price[y]
                gross_by_src[b] = gross_taken

            if reinvest_surplus and ssa_per_year[y] > spending_need:
                alive = (buckets["taxable"] + buckets["traditional"] + buckets["roth"]) > 0
                buckets["taxable"] = buckets["taxable"] + np.where(alive, ssa_per_year[y] - spending_need, 0.0)
            dist_values[:, y] = buckets["taxable"] + buckets["traditional"] + buckets["roth"]

            # Per-bucket medians taken separately don't add up when simulations
            # draw from different buckets in the same year, so report the split
            # of the simulation whose total gross draw is the median.
            total_y = gross_by_src["taxable"] + gross_by_src["traditional"] + gross_by_src["roth"]
            gross_by_year[:, y] = total_y / price[y]
            mid = int(np.argsort(total_y)[num_simulations // 2])
            age = (retirement_age + y) if retirement_age is not None else None
            funding_by_year.append(BridgeYearFunding(
                year=y + 1,
                age=age,
                ssa=_d(ssa_per_year[y] / price[y]),
                taxable=_d(gross_by_src["taxable"][mid] / price[y]),
                traditional=_d(gross_by_src["traditional"][mid] / price[y]),
                roth=_d(gross_by_src["roth"][mid] / price[y]),
            ))
            for b in order:
                if depletes_year[b] is None and props[b] > 0 and float(np.median(buckets[b])) <= 0.0:
                    depletes_year[b] = y + 1

        # ── Gap & Bridge numbers ───────────────────────────────────────────────
        # The bridge is the run of years before the FIRST Social Security dollar
        # arrives (whichever spouse claims earliest) — that's the stretch the
        # portfolio funds at 100%.
        first_ssa = next((y for y in range(withdrawal_years) if ssa_per_year[y] > 0), None)
        bridge_years = first_ssa if first_ssa is not None else 0

        # Annual after-tax spend the portfolio covers in the first retirement year.
        gap_annual = (max(float(spending_by_year[0]) - float(ssa_per_year[0]), 0.0) / start_price
                      if withdrawal_years else 0.0)

        # Total gross capital to fund the pre-SSA window = median across
        # simulations of each one's gross draws over the bridge years.
        bridge_number = (float(np.median(gross_by_year[:, :bridge_years].sum(axis=1)))
                         if bridge_years > 0 else 0.0)
        bridge_coverage = (float(np.mean(dist_values[:, bridge_years - 1] > 0) * 100.0)
                           if bridge_years > 0 else 100.0)

        buckets_out: list[BridgeBucket] = []
        for b in order:
            dy = depletes_year[b]
            dage = (retirement_age + dy - 1) if (dy is not None and retirement_age is not None) else None
            buckets_out.append(BridgeBucket(
                bucket=b,
                label=labels[b],
                start_balance=_d(props[b] * float(np.median(final_vals)) / start_price),
                end_median=_d(np.median(buckets[b]) / end_price),
                depletes_year=dy,
                depletes_age=dage,
            ))

        bridge = IncomeBridge(
            enabled=True,
            buckets=buckets_out,
            total_start=_d(np.median(final_vals) / start_price),
            gap_annual=_d(gap_annual),
            bridge_years=bridge_years,
            bridge_number=_d(bridge_number),
            bridge_coverage_pct=_d(bridge_coverage),
            deferred_tax_rate=_d(rates["traditional"] * 100.0),
            taxable_tax_rate=_d(rates["taxable"] * 100.0),
            taxes_total_median=_d(np.median(taxes_per_sim)),
            funding_by_year=funding_by_year,
        )
        return dist_values, bridge, net_drawn_per_sim

    def _simulate(
        self,
        label: str,
        allocation: dict[str, float],
        initial_value: float,
        years: int,
        num_simulations: int,
        goal_amount: float | None,
        regime: RegimeState | None = None,
        monthly_contribution: float = 0.0,
        withdrawal_rate: float = 0.0,
        withdrawal_years: int = 0,
        spending_pattern: str = "constant",
        smile_slow_pct: float = 80.0,
        smile_no_pct: float = 65.0,
        current_age: int | None = None,
        retirement_age: int | None = None,
        ssa_claiming_age: int | None = None,
        ssa_annual_benefit: float = 0.0,   # claiming-age-adjusted SSA, $/year (scalar fallback)
        ssa_fra_factor: float | None = None,
        ssa_schedule: list[float] | None = None,           # per-year household SSA $ (one or two earners)
        ssa_household: "SsaHousehold | None" = None,        # two-earner summary, attached to the result
        income_bridge: bool = False,                       # model account-type sequencing + taxes
        bucket_balances: dict[str, float] | None = None,   # today's taxable/traditional/roth $ balances
        deferred_tax_rate: float = 0.18,                   # effective tax on tax-deferred withdrawals (fraction)
        taxable_tax_rate: float = 0.10,                    # effective cap-gains drag on taxable withdrawals (fraction)
        inflation_rate: float = 0.0,                       # annual inflation (fraction); 0 = nominal model
        spending_fixed_annual: float | None = None,        # budget mode: fixed costs, today's $/yr
        spending_variable_annual: float | None = None,     # budget mode: variable costs, today's $/yr
        spending_scale: float = 1.0,                       # multiplies the budget (max-spending solver)
        reinvest_surplus: bool = False,                    # income above spending goes back into the portfolio
    ) -> MonteCarloResult:
        """
        Two-phase Monte Carlo: accumulation → distribution.

        Inflation: returns are nominal. With ``inflation_rate`` > 0, spending and
        Social Security (entered in today's dollars) grow with it — SSA gets the
        same COLA — and every reported dollar figure is converted back to
        today's dollars. At 0 the model is the original nominal one.

        Spending: either a withdrawal RATE (year-one spending = rate × median
        retirement balance, then growing with inflation; the smile scales all of
        it) or a BUDGET of fixed + variable costs in today's dollars (fixed costs
        stay level in real terms; the smile applies to the variable part only).

        Surplus: in a year when Social Security exceeds the spending need, the
        extra is ignored (not spent, not saved) unless ``reinvest_surplus``, in
        which case it is added back to the portfolio (taxable bucket under the
        income bridge).

        Accumulation phase  (years 1 … years)
        ─────────────────────────────────────
        GBM with optional Markov regime conditioning (first 2 years) and
        mid-year deposit approximation:
            V(t+1) = V(t) × gf  +  C_annual × √gf

        Distribution phase  (years+1 … years+withdrawal_years)
        ───────────────────────────────────────────────────────
        Activated when withdrawal_rate > 0 and withdrawal_years > 0.
        Two spending patterns are supported:

        Constant: classic flat withdrawal computed once per simulation from
        the ending accumulation value (4% rule framing):
            annual_withdrawal_i  = V_end_i × (withdrawal_rate / 100)

        Spending Smile: Go-Go / Slow-Go / No-Go phase step-downs.
        Distribution years are split into thirds; each phase applies a
        multiplier to the initial Go-Go withdrawal amount:
            effective_wdraw_y = annual_wdraw_i × smile_mults[y]

        A portfolio reaching 0 stays at 0 ("ruin" event).  The survival rate
        is the fraction of simulations that end the distribution phase > 0.
        """
        # Portfolio expected (simple) annual return and correlation-aware volatility.
        port_mu, port_sigma = portfolio_return_and_vol(allocation)

        def _log_drift(mu_simple: float, sig: float) -> float:
            # Log-normal annual growth whose mean is 1 + mu_simple:
            # E[exp(N(m, σ²))] = exp(m + σ²/2) = 1 + mu  ⇒  m = ln(1 + mu) − σ²/2.
            return float(np.log1p(mu_simple)) - 0.5 * sig ** 2

        # Simulate: log-normal Geometric Brownian Motion (annual steps).
        rng = np.random.default_rng(seed=42)
        drift = _log_drift(port_mu, port_sigma)
        infl = max(0.0, float(inflation_rate))

        def _price(t: float) -> float:
            """Price level t years from today (today = 1)."""
            return (1.0 + infl) ** t
        # Shape: (num_simulations, years)
        log_returns = rng.normal(drift, port_sigma, size=(num_simulations, years))

        # ── Regime conditioning — override first 2 years ──────────────────────
        # Blend schedule: year 0 = 100 % regime, year 1 = 50 %, year 2+ = base
        regime_context = "Base"
        regime_adj: dict[str, float] | None = None
        blend_schedule = [1.0, 0.5]  # regime weight in sim years 1 and 2, then base
        if regime is not None:
            regime_context = regime.current_regime
            REGIME_ADJ = {
                "Bull":     {"mu_delta": +0.02, "sigma_factor": 0.90},
                "Sideways": {"mu_delta":  0.00, "sigma_factor": 1.00},
                "Bear":     {"mu_delta": -0.03, "sigma_factor": 1.20},
            }
            adj = REGIME_ADJ.get(regime.current_regime, {"mu_delta": 0.0, "sigma_factor": 1.0})
            regime_adj = adj
            for y, blend in enumerate(blend_schedule[:years]):
                if blend == 0:
                    continue
                adj_mu     = port_mu + blend * adj["mu_delta"]
                adj_sigma  = port_sigma * (1.0 + blend * (adj["sigma_factor"] - 1.0))
                adj_drift  = _log_drift(adj_mu, adj_sigma)
                log_returns[:, y] = rng.normal(adj_drift, adj_sigma, num_simulations)

        # ── Compound log-returns → portfolio values ───────────────────────────
        # years=0 means immediate retirement: skip accumulation entirely.
        trajectories: list[SimulationOutcome] = []
        if years == 0:
            final_vals = np.full(num_simulations, initial_value, dtype=float)
        else:
            growth_factors = np.exp(log_returns)   # shape: (num_simulations, years)
            annual_contrib = monthly_contribution * 12.0

            if annual_contrib > 0:
                # Iterative path: each year applies growth then adds mid-year contributions.
                # Mid-year approximation: contributions are assumed to arrive uniformly
                # throughout the year, so on average they earn √gf (6-month growth).
                #   V(t+1) = V(t) × gf  +  C_annual × √gf
                port = np.full(num_simulations, initial_value, dtype=float)
                yearly_values = np.empty((num_simulations, years), dtype=float)
                for y in range(years):
                    gf = growth_factors[:, y]
                    port = port * gf + annual_contrib * np.sqrt(gf)
                    yearly_values[:, y] = port
                cumulative = yearly_values
            else:
                cumulative = np.cumprod(growth_factors, axis=1) * initial_value

            for y in range(years):
                vals = cumulative[:, y] / _price(y + 1)          # today's dollars
                trajectories.append(SimulationOutcome(
                    year=y + 1,
                    p10=Decimal(str(round(float(np.percentile(vals, 10)), 2))),
                    p25=Decimal(str(round(float(np.percentile(vals, 25)), 2))),
                    median=Decimal(str(round(float(np.median(vals)), 2))),
                    p75=Decimal(str(round(float(np.percentile(vals, 75)), 2))),
                    p90=Decimal(str(round(float(np.percentile(vals, 90)), 2))),
                ))

            final_vals = cumulative[:, -1]
        prob_goal = None
        if goal_amount:   # the goal is in today's dollars
            prob_goal = float(np.mean(final_vals / _price(years) >= goal_amount) * 100)

        # ── Distribution / withdrawal phase ───────────────────────────────────
        withdrawal_trajectories: list[SimulationOutcome] = []
        withdrawal_annual_amount = None
        withdrawal_total         = None
        withdrawal_final_median  = None
        withdrawal_final_p10     = None
        withdrawal_final_p90     = None
        portfolio_survival_rate  = None
        go_go_yrs   = 0
        slow_go_yrs = 0
        ssa_annual_out: Decimal | None = None
        ssa_monthly_out: Decimal | None = None
        ssa_total_out: Decimal | None = None
        bridge_years_out: int | None = None
        net_draw_after_ssa_out: Decimal | None = None
        income_bridge_out: IncomeBridge | None = None

        budget_mode = (spending_fixed_annual is not None or spending_variable_annual is not None)
        fixed_real = max(0.0, float(spending_fixed_annual or 0.0)) * spending_scale
        variable_real = max(0.0, float(spending_variable_annual or 0.0)) * spending_scale
        implied_rate_out: Decimal | None = None
        if budget_mode and fixed_real + variable_real <= 0:
            budget_mode = False

        if (withdrawal_rate > 0 or budget_mode) and withdrawal_years > 0:
            # One spending schedule for every simulation (scaling each path's
            # spending to its own balance would hide accumulation risk).
            median_final = float(np.median(final_vals))

            # ── Spending Smile: build per-year multipliers ────────────────────
            # Split distribution years into thirds: Go-Go (1.0×) → Slow-Go → No-Go.
            # Any remainder goes to Go-Go then Slow-Go (10 yrs → 4/3/3, 4 → 2/1/1).
            import math as _math
            smile_mults = np.ones(withdrawal_years)
            go_go_yrs = slow_go_yrs = 0
            if spending_pattern == "smile" and withdrawal_years > 0:
                go_go_yrs   = _math.ceil(withdrawal_years / 3)
                no_go_yrs   = withdrawal_years // 3
                slow_go_yrs = withdrawal_years - go_go_yrs - no_go_yrs
                for y in range(withdrawal_years):
                    if y < go_go_yrs:
                        smile_mults[y] = 1.0
                    elif y < go_go_yrs + slow_go_yrs:
                        smile_mults[y] = smile_slow_pct / 100.0
                    else:
                        smile_mults[y] = smile_no_pct / 100.0

            # Price level during each retirement year, and that year's spending
            # need in nominal dollars (what the portfolio actually has to pay).
            price_by_year = np.array([_price(years + y) for y in range(withdrawal_years)])
            if budget_mode:
                need_real = fixed_real + variable_real * smile_mults        # smile on variable only
            else:
                year1_nominal = median_final * (withdrawal_rate / 100.0)
                need_real = (year1_nominal / _price(years)) * smile_mults   # level real spending × smile
            need_nominal = need_real * price_by_year

            # Fresh base-case growth factors for the distribution phase. The
            # regime window is the first 2 years of the whole simulation, so a
            # short (or no) accumulation phase carries it into retirement.
            dist_log_rets = rng.normal(_log_drift(port_mu, port_sigma), port_sigma,
                                       size=(num_simulations, withdrawal_years))
            if regime_adj is not None:
                for y in range(withdrawal_years):
                    t = years + y
                    if t >= len(blend_schedule):
                        break
                    blend = blend_schedule[t]
                    adj_sigma = port_sigma * (1.0 + blend * (regime_adj["sigma_factor"] - 1.0))
                    dist_log_rets[:, y] = rng.normal(
                        _log_drift(port_mu + blend * regime_adj["mu_delta"], adj_sigma),
                        adj_sigma, num_simulations)
            dist_gf = np.exp(dist_log_rets)

            # ── Social Security income offset ──────────────────────────────────
            # Before any benefit starts the portfolio funds 100% of spending (the
            # "income bridge"); once SSA is flowing it covers part of each year's
            # need and the portfolio only draws the remaining gap. The per-year
            # schedule is supplied by run_monte_carlo (one or two earners); the
            # scalar fallback keeps direct _simulate callers (and tests) working.
            ssa_per_year = np.zeros(withdrawal_years)       # today's dollars, as entered
            if ssa_schedule is not None:
                arr = np.asarray(ssa_schedule, dtype=float)
                ssa_per_year[:len(arr)] = arr[:withdrawal_years]
            elif (
                ssa_annual_benefit > 0
                and retirement_age is not None
                and ssa_claiming_age is not None
            ):
                for y in range(withdrawal_years):
                    if retirement_age + y >= ssa_claiming_age:
                        ssa_per_year[y] = ssa_annual_benefit

            ssa_nominal = ssa_per_year * price_by_year      # benefits get the same COLA

            # Bucket balances must be present and positive for the bridge to run.
            bridge_active = (
                income_bridge
                and bucket_balances is not None
                and sum(max(0.0, float(v)) for v in bucket_balances.values()) > 0
            )

            if bridge_active:
                # Account-level withdrawal sequencing (taxable → traditional → Roth)
                # with a simple effective-tax gross-up. Answers "which accounts
                # fund the pre-SSA years". Treats the smile-adjusted withdrawal as
                # the household's *after-tax* spending need.
                dist_values, income_bridge_out, drawn_per_sim = self._run_income_bridge(
                    final_vals=final_vals,
                    reinvest_surplus=reinvest_surplus,
                    spending_by_year=need_nominal,
                    price_by_year=price_by_year,
                    end_price=_price(years + withdrawal_years),
                    ssa_per_year=ssa_nominal,
                    dist_gf=dist_gf,
                    bucket_balances=bucket_balances,
                    deferred_tax_rate=deferred_tax_rate,
                    taxable_tax_rate=taxable_tax_rate,
                    withdrawal_years=withdrawal_years,
                    num_simulations=num_simulations,
                    retirement_age=retirement_age,
                    ssa_claiming_age=ssa_claiming_age,
                )
            else:
                port_d = final_vals.copy()
                dist_values = np.empty((num_simulations, withdrawal_years), dtype=float)
                drawn_per_sim = np.zeros(num_simulations)
                for y in range(withdrawal_years):
                    # End-of-year: portfolio grows first; the year's spending need
                    # is met from SSA first, then the portfolio covers the rest.
                    portfolio_draw = max(need_nominal[y] - ssa_nominal[y], 0.0)
                    surplus = max(ssa_nominal[y] - need_nominal[y], 0.0) if reinvest_surplus else 0.0
                    grown = port_d * dist_gf[:, y]
                    drawn_per_sim += np.minimum(grown, portfolio_draw) / price_by_year[y]   # today's $
                    port_d = np.maximum(grown - portfolio_draw, 0.0)
                    if surplus:
                        port_d = np.where(port_d > 0, port_d + surplus, 0.0)   # ruin stays ruin
                    dist_values[:, y] = port_d

            # First point: retirement starting value = end of accumulation.
            # This anchors the orange distribution fan to the exact point where
            # the blue accumulation fan ends, so the chart shows a clear kink
            # when contributions stop and withdrawals begin.
            start_real = final_vals / _price(years)
            withdrawal_trajectories.append(SimulationOutcome(
                year=years,  # same index as last accumulation year
                p10=Decimal(str(round(float(np.percentile(start_real, 10)), 2))),
                p25=Decimal(str(round(float(np.percentile(start_real, 25)), 2))),
                median=Decimal(str(round(float(np.median(start_real)), 2))),
                p75=Decimal(str(round(float(np.percentile(start_real, 75)), 2))),
                p90=Decimal(str(round(float(np.percentile(start_real, 90)), 2))),
            ))

            for y in range(withdrawal_years):
                vals_d = dist_values[:, y] / _price(years + y + 1)        # today's dollars
                withdrawal_trajectories.append(SimulationOutcome(
                    year=years + y + 1,   # continues year-count from accumulation
                    p10=Decimal(str(round(float(np.percentile(vals_d, 10)), 2))),
                    p25=Decimal(str(round(float(np.percentile(vals_d, 25)), 2))),
                    median=Decimal(str(round(float(np.median(vals_d)), 2))),
                    p75=Decimal(str(round(float(np.percentile(vals_d, 75)), 2))),
                    p90=Decimal(str(round(float(np.percentile(vals_d, 90)), 2))),
                ))

            med_wdraw = float(need_real[0])                 # year-one spending, today's $
            final_d   = dist_values[:, -1] / _price(years + withdrawal_years)
            survival  = float(np.mean(final_d > 0) * 100)
            # Year-one spending as a share of the median retirement balance.
            if median_final > 0:
                implied_rate_out = Decimal(str(round(need_nominal[0] / median_final * 100, 2)))

            # Total actually drawn from the portfolio (median across sims): net of
            # the spending Social Security covered, and stopping at depletion.
            total_distributed = float(np.median(drawn_per_sim))

            # ── Social Security summary (deterministic across sims) ────────────
            # Derived from the per-year array so one or two earners are handled
            # identically: the headline annual is the household peak once all
            # streams are flowing, and the bridge is the years before the first
            # SSA dollar arrives.
            if ssa_per_year.any():
                peak_ssa = float(ssa_per_year.max())
                ssa_annual_out  = Decimal(str(round(peak_ssa, 2)))
                ssa_monthly_out = Decimal(str(round(peak_ssa / 12.0, 2)))
                ssa_total_out   = Decimal(str(round(float(np.sum(ssa_per_year)), 2)))
                first_ssa = next((y for y in range(withdrawal_years) if ssa_per_year[y] > 0), None)
                bridge_years_out = first_ssa if first_ssa is not None else 0
                # Portfolio's own Go-Go-level draw once SSA is flowing.
                net_draw_after_ssa_out = Decimal(str(round(max(med_wdraw - peak_ssa, 0.0), 2)))

            withdrawal_annual_amount = Decimal(str(round(med_wdraw, 2)))
            withdrawal_total         = Decimal(str(round(total_distributed, 2)))
            withdrawal_final_median  = Decimal(str(round(float(np.median(final_d)), 2)))
            withdrawal_final_p10     = Decimal(str(round(float(np.percentile(final_d, 10)), 2)))
            withdrawal_final_p90     = Decimal(str(round(float(np.percentile(final_d, 90)), 2)))
            portfolio_survival_rate  = Decimal(str(round(survival, 1)))

        return MonteCarloResult(
            label=label,
            initial_value=Decimal(str(round(initial_value, 2))),
            num_simulations=num_simulations,
            years=years,
            trajectories=trajectories,
            final_median=Decimal(str(round(float(np.median(final_vals)) / _price(years), 2))),
            final_p10=Decimal(str(round(float(np.percentile(final_vals, 10)) / _price(years), 2))),
            final_p90=Decimal(str(round(float(np.percentile(final_vals, 90)) / _price(years), 2))),
            probability_of_goal=Decimal(str(round(prob_goal, 1))) if prob_goal is not None else None,
            goal_amount=Decimal(str(goal_amount)) if goal_amount else None,
            regime_context=regime_context,
            monthly_contribution=Decimal(str(round(monthly_contribution, 2))),
            withdrawal_rate=(implied_rate_out if budget_mode and implied_rate_out is not None
                             else Decimal(str(round(withdrawal_rate, 2)))),
            inflation_rate=Decimal(str(round(infl * 100, 2))),
            spending_mode="budget" if budget_mode else "rate",
            spending_fixed_monthly=(Decimal(str(round(fixed_real / 12, 2))) if budget_mode else None),
            spending_variable_monthly=(Decimal(str(round(variable_real / 12, 2))) if budget_mode else None),
            implied_withdrawal_rate=implied_rate_out,
            withdrawal_years=withdrawal_years,
            withdrawal_trajectories=withdrawal_trajectories,
            withdrawal_annual_amount=withdrawal_annual_amount,
            withdrawal_total=withdrawal_total,
            withdrawal_final_median=withdrawal_final_median,
            withdrawal_final_p10=withdrawal_final_p10,
            withdrawal_final_p90=withdrawal_final_p90,
            portfolio_survival_rate=portfolio_survival_rate,
            spending_pattern=spending_pattern,
            smile_slow_pct=smile_slow_pct if spending_pattern == "smile" else None,
            smile_no_pct=smile_no_pct if spending_pattern == "smile" else None,
            smile_go_go_years=go_go_yrs,
            smile_slow_go_years=slow_go_yrs,
            current_age=current_age,
            retirement_age=retirement_age,
            ssa_claiming_age=ssa_claiming_age if ssa_annual_out is not None else None,
            ssa_monthly_benefit=ssa_monthly_out,
            ssa_annual_benefit=ssa_annual_out,
            ssa_fra_factor=ssa_fra_factor if ssa_annual_out is not None else None,
            ssa_total_benefit=ssa_total_out,
            bridge_years=bridge_years_out,
            net_draw_after_ssa=net_draw_after_ssa_out,
            income_bridge=income_bridge_out,
            ssa_household=ssa_household,
        )

    # ── Correlation Matrix ───────────────────────────────────────────────

    def get_correlation_matrix(self, lookback_days: int = 252) -> CorrelationMatrix:
        """
        Pairwise Pearson correlation of simple daily returns, aligned by date,
        for holdings with price history over the lookback (trading days).
        Private / illiquid positions without sufficient history are excluded.
        """
        import math

        summary = self.portfolio_svc.get_summary()
        end_date = date.today()
        start_date = end_date - timedelta(days=math.ceil(lookback_days * 365 / 252))

        symbols = sorted({h.symbol for h in summary.holdings if h.symbol})
        rets = self._aligned_returns(self._bulk_price_series(symbols, start_date, end_date))
        if len(rets) < 30 or rets.shape[1] < 2:
            return CorrelationMatrix(
                symbols=list(rets.columns) if len(rets) >= 30 else [],
                matrix=[],
                lookback_days=lookback_days,
                as_of=end_date,
            )

        symbol_list = sorted(rets.columns)
        corr = rets[symbol_list].corr().values

        matrix_out = [
            [Decimal(str(round(float(corr[i, j]), 4))) for j in range(len(symbol_list))]
            for i in range(len(symbol_list))
        ]
        return CorrelationMatrix(
            symbols=symbol_list,
            matrix=matrix_out,
            lookback_days=lookback_days,
            as_of=end_date,
        )

    # ── Time-Weighted & Money-Weighted Returns ───────────────────────────

    def get_return_metrics(self) -> ReturnMetrics:
        """
        Trailing returns of the CURRENT holdings (a backtest, not a true
        time-weighted return: the app has no record of past portfolio values or
        external cash flows to chain-link).

        Each window values today's positions buy-and-hold from the window start
        (price on or before that date) to the latest price. Cash and other
        stable-value positions count at 0% return. A position without price
        history back to the window start is left out of that window and the
        rest are re-weighted; ``history_coverage_pct`` reports how much of the
        portfolio the 1-year figure covers. Returns are annualised only for
        windows of at least one year.
        """
        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        if total_mv <= 0:
            return ReturnMetrics()

        end_date = date.today()
        positions: dict[str, float] = {}
        flat_mv = 0.0
        uncovered_mv = 0.0
        for h in summary.holdings:
            mv = float(h.market_value)
            sym = h.symbol.upper() if h.symbol else None
            if sym and sym in STABLE_VALUE_SYMBOLS:
                flat_mv += mv
            elif h.symbol:
                positions[h.symbol] = positions.get(h.symbol, 0.0) + mv
            else:
                uncovered_mv += mv

        prices = self._bulk_price_series(sorted(positions), end_date - timedelta(days=5 * 366 + 10),
                                         end_date, min_coverage=0.0)
        slack = timedelta(days=7)

        def _window(start: date) -> tuple[float, float] | None:
            """(cumulative return, weight covered) from ``start`` to today."""
            start_val = end_val = flat_mv
            for sym, mv in positions.items():
                ser = prices.get(sym)
                if ser is None or ser.index[0] > start + slack:
                    continue
                p0 = float(ser[ser.index <= start].iloc[-1]) if (ser.index <= start).any() else float(ser.iloc[0])
                p1 = float(ser.iloc[-1])
                if p0 <= 0:
                    continue
                end_val += mv
                start_val += mv * p0 / p1
            if end_val <= flat_mv or start_val <= 0:
                return None
            return end_val / start_val - 1, end_val / total_mv

        def _ann(cum: float, years: float) -> Decimal:
            r = (1 + cum) ** (1 / years) - 1 if years >= 1 else cum
            return Decimal(str(round(r * 100, 2)))

        out: dict[str, Decimal | None] = {}
        coverage = None
        for key, yrs in (("twr_1yr", 1), ("twr_3yr", 3), ("twr_5yr", 5)):
            res = _window(end_date - timedelta(days=round(365.25 * yrs)))
            out[key] = _ann(res[0], yrs) if res else None
            if key == "twr_1yr" and res:
                coverage = Decimal(str(round(res[1] * 100, 1)))

        # "Since": the earliest date by which positions making up ≥90% of the
        # priced portfolio have history.
        inception = since = None
        starts = sorted((prices[sym].index[0], mv) for sym, mv in positions.items() if sym in prices)
        if starts:
            need = 0.9 * (sum(positions.values()) + flat_mv) - flat_mv
            acc = 0.0
            for d, mv in starts:
                acc += mv
                if acc >= need:
                    since = d
                    break
            since = since or starts[-1][0]
            res = _window(since)
            if res:
                inception = _ann(res[0], (end_date - since).days / 365.25)

        return ReturnMetrics(
            twr_1yr=out["twr_1yr"], twr_3yr=out["twr_3yr"], twr_5yr=out["twr_5yr"],
            twr_inception=inception, annualized_since=since,
            history_coverage_pct=coverage,
        )

    def get_period_returns(self) -> list[dict]:
        """Trailing-window total return of the CURRENT holdings — 3M / 6M / 12M /
        YTD — as both a percentage and a dollar gain.

        Each holding is valued buy-and-hold: its window-ago price vs. today's,
        scaled by current quantity. Cash, private, and positions without enough
        price history for a window are held flat (0% over that window). Price
        history comes from the DB when dense enough, otherwise a single bulk
        yfinance download. Cached ~60 min so the dashboard stays responsive.
        """
        from datetime import datetime as _dt

        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        if total_mv <= 0:
            return []

        cache_key = _dt.now().strftime("%Y%m%d%H")
        if cache_key in _PERIOD_RETURNS_CACHE:
            return _PERIOD_RETURNS_CACHE[cache_key]
        _PERIOD_RETURNS_CACHE.clear()  # drop prior hours

        end_date = date.today()
        symbols = list({
            h.symbol for h in summary.holdings
            if h.symbol and h.symbol.upper() not in STABLE_VALUE_SYMBOLS
        })
        all_returns = (
            self._bulk_daily_returns(symbols, end_date - timedelta(days=430), end_date)
            if symbols else {}
        )

        ytd_days = round((end_date - date(end_date.year, 1, 1)).days * 252 / 365)
        windows = [
            ("3M", "3-Month", 63), ("6M", "6-Month", 126),
            ("12M", "12-Month", 252), ("YTD", "YTD", max(1, ytd_days)),
        ]

        out: list[dict] = []
        for key, label, n in windows:
            start_val = 0.0
            any_data = False
            for h in summary.holdings:
                mv = float(h.market_value)
                cum = 0.0  # flat default — cash / private / insufficient history
                sym = h.symbol.upper() if h.symbol else None
                if sym and sym not in STABLE_VALUE_SYMBOLS:
                    rets = all_returns.get(h.symbol)
                    if rets is not None and len(rets) >= n:
                        cum = float(np.prod(1 + rets[-n:])) - 1
                        any_data = True
                start_val += mv / (1 + cum) if cum > -1 else mv
            if not any_data or start_val <= 0:
                out.append({"key": key, "label": label, "available": False})
                continue
            gain = total_mv - start_val
            out.append({
                "key": key, "label": label, "available": True,
                "pct": round(gain / start_val * 100, 2),
                "dollar": round(gain, 2), "up": gain >= 0,
            })

        _PERIOD_RETURNS_CACHE[cache_key] = out
        return out

    # ── Seed ────────────────────────────────────────────────────────────

    def seed_default_profiles(self) -> None:
        """Create the 5 system investment profiles if they don't exist."""
        for profile_data in DEFAULT_PROFILES:
            existing = (
                self.session.query(InvestmentProfile)
                .filter(InvestmentProfile.name == profile_data["name"], InvestmentProfile.is_system == True)
                .first()
            )
            if not existing:
                p = InvestmentProfile(
                    name=profile_data["name"],
                    risk_tolerance=RiskToleranceEnum(profile_data["risk_tolerance"]),
                    is_system=True,
                    is_active=profile_data.get("is_active", False),  # only one true (Balanced)
                    description=profile_data["description"],
                    target_allocations_json=json.dumps(
                        {k: str(v) for k, v in profile_data["allocations"].items()}
                    ),
                )
                self.session.add(p)
        self.session.flush()

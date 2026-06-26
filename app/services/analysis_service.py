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
    SsaPerson,
    RegimeState,
    ReturnMetrics,
    RiskMetrics,
    SimulationOutcome,
)
from app.services.market_data import MarketDataService, STABLE_VALUE_SYMBOLS
from app.services.portfolio_service import PortfolioService
from app.services.render_guard import network_allowed

# Process-lifetime cache for trailing-window returns, refreshed hourly (the
# dashboard's landing-page call must stay snappy; period returns barely move
# intraday). Keyed by "YYYYMMDDHH".
_PERIOD_RETURNS_CACHE: dict[str, list[dict]] = {}


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
        """Compute portfolio risk metrics from historical price data."""
        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        if total_mv <= 0:
            return self._empty_risk_metrics(lookback_days, risk_free_rate)

        # Collect per-holding daily returns
        end_date = date.today()
        start_date = end_date - timedelta(days=lookback_days)
        holdings_returns: list[tuple[dict, np.ndarray]] = []
        weights: list[float] = []

        # Bulk-fetch all symbols at once (single yfinance call, orders of magnitude faster)
        symbols_needed = [h.symbol for h in summary.holdings if h.symbol]
        bulk_returns = self._bulk_daily_returns(symbols_needed + ["SPY"], start_date, end_date)
        spy_returns = bulk_returns.get("SPY")

        for h in summary.holdings:
            w = float(h.market_value) / total_mv
            info = {
                "asset_id": h.asset_id,
                "symbol": h.symbol,
                "name": h.name,
                "asset_class": h.asset_class,
                "weight": w,
            }
            rets = bulk_returns.get(h.symbol) if h.symbol else None
            if rets is not None and len(rets) > 20:
                holdings_returns.append((info, rets))
                weights.append(w)
                continue
            # Private asset or insufficient data — use assumed vol
            assumed = ASSET_CLASS_ASSUMPTIONS.get(h.asset_class, ASSET_CLASS_ASSUMPTIONS["other"])
            daily_vol = assumed["volatility"] / np.sqrt(252)
            fake_rets = np.random.normal(assumed["return"] / 252, daily_vol, lookback_days)
            holdings_returns.append((info, fake_rets))
            weights.append(w)

        if not holdings_returns:
            return self._empty_risk_metrics(lookback_days, risk_free_rate)

        # Align returns to common length
        min_len = min(len(r) for _, r in holdings_returns)
        aligned = np.column_stack([r[-min_len:] for _, r in holdings_returns])
        w_arr = np.array(weights)
        w_arr = w_arr / w_arr.sum()  # normalize

        # Portfolio daily returns
        port_daily = aligned @ w_arr

        # Build a trading-day date index for the aligned window so that
        # drawdown dates are accurate (return indices ≠ calendar day offsets).
        # We generate the sequence of trading days that yfinance would have
        # returned for [start_date, end_date], then take the last min_len.
        import pandas as pd
        trading_days = pd.bdate_range(start=str(start_date), end=str(end_date))
        # bdate_range gives open-interval dates; returns are diff of prices so
        # there is one fewer return than price point — align to min_len.
        if len(trading_days) > min_len:
            trading_days = trading_days[-min_len:]

        # Annualized metrics
        port_vol = float(np.std(port_daily) * np.sqrt(252))
        port_mean = float(np.mean(port_daily) * 252)
        sharpe = (port_mean - risk_free_rate) / port_vol if port_vol > 0 else 0.0

        # Max drawdown — use trading_day index for accurate period labels
        cumulative = np.cumprod(1 + port_daily)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max
        max_dd = float(np.min(drawdowns))
        dd_end_idx = int(np.argmin(drawdowns))
        dd_start_idx = int(np.argmax(cumulative[:dd_end_idx + 1])) if dd_end_idx > 0 else 0
        if len(trading_days) > dd_end_idx:
            dd_start_date = trading_days[dd_start_idx].date()
            dd_end_date   = trading_days[dd_end_idx].date()
        else:
            dd_start_date = start_date + timedelta(days=dd_start_idx)
            dd_end_date   = start_date + timedelta(days=dd_end_idx)
        dd_period = f"{dd_start_date} to {dd_end_date}"

        # Portfolio beta vs SPY
        port_beta = 0.0
        if spy_returns is not None and len(spy_returns) >= min_len:
            spy_aligned = spy_returns[-min_len:]
            cov = np.cov(port_daily, spy_aligned)
            spy_var = cov[1, 1]
            port_beta = float(cov[0, 1] / spy_var) if spy_var > 0 else 0.0

        # Per-position risk
        position_risks = []
        for i, (info, rets) in enumerate(holdings_returns):
            rets_aligned = rets[-min_len:]
            pos_vol = float(np.std(rets_aligned) * np.sqrt(252))
            pos_beta = None
            if spy_returns is not None and len(spy_returns) >= min_len:
                spy_a = spy_returns[-min_len:]
                cov_pos = np.cov(rets_aligned, spy_a)
                spy_v = cov_pos[1, 1]
                pos_beta = float(cov_pos[0, 1] / spy_v) if spy_v > 0 else None

            # Risk contribution % = w_i * Cov(r_i, r_p)_annual / Var(r_p)_annual * 100
            # This formula ensures contributions sum exactly to 100%.
            # Both covariance and variance are annualised by multiplying daily values by 252.
            cov_with_port_daily = float(np.cov(rets_aligned, port_daily)[0, 1])
            cov_with_port_annual = cov_with_port_daily * 252
            port_var = port_vol ** 2  # port_vol is already annualised
            risk_contrib = (w_arr[i] * cov_with_port_annual / port_var * 100) if port_var > 0 else 0

            position_risks.append(PositionRisk(
                asset_id=info["asset_id"],
                symbol=info["symbol"],
                name=info["name"],
                asset_class=info["asset_class"],
                weight_pct=Decimal(str(round(info["weight"] * 100, 2))),
                annualized_volatility=Decimal(str(round(pos_vol * 100, 2))),
                beta=Decimal(str(round(pos_beta, 3))) if pos_beta is not None else None,
                risk_contribution_pct=Decimal(str(round(risk_contrib, 2))),
            ))

        # Concentration
        sorted_weights = sorted([float(h.weight_pct) for h in summary.holdings], reverse=True)
        top5 = sum(sorted_weights[:5])
        top10 = sum(sorted_weights[:10])
        # HHI in DOJ convention: weights are expressed as percentages (0–100),
        # so a single-holding portfolio → 100² = 10,000 (maximum concentration).
        hhi = sum(w ** 2 for w in sorted_weights)
        largest = max(summary.holdings, key=lambda h: h.weight_pct)

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
            largest_position=largest.symbol or largest.name,
            largest_position_weight_pct=largest.weight_pct,
            hhi_index=Decimal(str(round(hhi, 2))),
            hhi_lookthrough=Decimal(str(round(lt_hhi, 2))) if lt_hhi is not None else None,
            hhi_lookthrough_positions=lt_positions,
            hhi_lookthrough_coverage_pct=Decimal(str(round(lt_coverage, 1))) if lt_coverage is not None else None,
            hhi_lookthrough_top=lt_top,
        )

        # Asset class risk (use same Var-normalised formula as position level)
        ac_risks = []
        ac_groups: dict[str, list[int]] = {}
        for i, (info, _) in enumerate(holdings_returns):
            ac_groups.setdefault(info["asset_class"], []).append(i)

        port_var = port_vol ** 2  # annualised variance (reused below for VaR too)
        for ac, indices in ac_groups.items():
            ac_weight = sum(w_arr[i] for i in indices)
            ac_rets = sum(aligned[-min_len:, i] * w_arr[i] for i in indices) / ac_weight if ac_weight > 0 else np.zeros(min_len)
            ac_vol = float(np.std(ac_rets) * np.sqrt(252))
            cov_ac_daily = float(np.cov(ac_rets, port_daily)[0, 1])
            cov_ac_annual = cov_ac_daily * 252
            contrib = (ac_weight * cov_ac_annual / port_var * 100) if port_var > 0 else 0

            ac_risks.append(AssetClassRisk(
                asset_class=ac,
                weight_pct=Decimal(str(round(float(ac_weight) * 100, 2))),
                annualized_volatility=Decimal(str(round(ac_vol * 100, 2))),
                contribution_to_risk=Decimal(str(round(contrib, 2))),
            ))

        # Value-at-Risk and CVaR (Historical Simulation — no normality assumption)
        total_mv_dec = Decimal(str(round(total_mv, 2)))
        sorted_rets = np.sort(port_daily)
        var_95 = cvar_95 = var_99 = cvar_99 = None
        if len(sorted_rets) >= 20:
            idx_95 = max(0, int(np.floor(0.05 * len(sorted_rets))))
            idx_99 = max(0, int(np.floor(0.01 * len(sorted_rets))))
            var_95_ret = float(sorted_rets[idx_95])
            var_99_ret = float(sorted_rets[idx_99])
            cvar_95_ret = float(np.mean(sorted_rets[:idx_95 + 1]))
            cvar_99_ret = float(np.mean(sorted_rets[:idx_99 + 1]))
            # Express as positive dollar losses
            var_95 = Decimal(str(round(abs(var_95_ret) * total_mv, 2)))
            cvar_95 = Decimal(str(round(abs(cvar_95_ret) * total_mv, 2)))
            var_99 = Decimal(str(round(abs(var_99_ret) * total_mv, 2)))
            cvar_99 = Decimal(str(round(abs(cvar_99_ret) * total_mv, 2)))

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
        )

    def _bulk_daily_returns(
        self, symbols: list[str], start_date: date, end_date: date
    ) -> dict[str, np.ndarray | None]:
        """
        Fetch daily returns for multiple symbols in a single yfinance call.
        Falls back to DB price history when available. Much faster than per-ticker calls.
        """
        if not symbols:
            return {}

        results: dict[str, np.ndarray | None] = {}

        # First: use DB price history (instant, no network) — but only when
        # the DB has enough rows to meaningfully cover the requested lookback.
        # ~70% of calendar days are trading days, so require at least 60% coverage
        # of *expected trading days* (calendar_days × 0.70 × 0.60 ≈ 42%).
        # The hard minimum is 10 (not 30) so short lookbacks (e.g. 30 days → only
        # ~22 trading days available) can still use DB data. The old max(30, ...)
        # caused short-window lookbacks to always fall through to yfinance, which
        # then returns 9 NaN weekend rows out of 30 → only 20 valid returns →
        # the ">20" holding threshold silently fell through to assumed returns for
        # every holding except crypto (BTC trades 7 days/week).
        calendar_days = max(1, (end_date - start_date).days)
        min_rows_required = max(10, int(calendar_days * 0.60))

        from app.models.asset import Asset as AssetModel
        db_assets = {
            a.symbol: a.id
            for a in self.session.query(AssetModel)
            .filter(AssetModel.symbol.in_(symbols))
            .all()
            if a.symbol
        }
        for sym, asset_id in db_assets.items():
            prices_rows = (
                self.session.query(AssetPrice)
                .filter(
                    AssetPrice.asset_id == asset_id,
                    AssetPrice.price_date >= start_date,
                    AssetPrice.price_date <= end_date,
                )
                .order_by(AssetPrice.price_date)
                .all()
            )
            if len(prices_rows) >= min_rows_required:
                prices = np.array([float(p.close_price) for p in prices_rows])
                rets = np.diff(prices) / prices[:-1]
                # Sanity-check: if any single-day return exceeds ±99%, the DB
                # history is corrupted (e.g. wrong magnitude). Fall back to yfinance.
                if np.any(np.abs(rets) > 0.99):
                    continue
                results[sym] = rets

        # Second: bulk-download remaining symbols from yfinance — but ONLY in an
        # explicit-refresh context. During an ordinary page render we never hit
        # the network (it scales with portfolio size and would hang the page);
        # symbols without stored history simply fall back to assumed returns.
        missing = [s for s in symbols if s not in results]
        if missing and network_allowed():
            try:
                import yfinance as yf
                raw = yf.download(
                    missing,
                    start=str(start_date),
                    end=str(end_date),
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
                if not raw.empty:
                    close = raw["Close"] if "Close" in raw.columns else raw
                    if hasattr(close, "columns"):
                        for sym in missing:
                            if sym in close.columns:
                                col = close[sym].dropna()
                                if len(col) >= 10:
                                    p = col.values
                                    results[sym] = np.diff(p) / p[:-1]
                    else:
                        # Single ticker — close is a Series
                        if len(missing) == 1:
                            col = close.dropna()
                            if len(col) >= 10:
                                p = col.values
                                results[missing[0]] = np.diff(p) / p[:-1]
            except Exception:
                pass

        return results

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
            try:
                fd = yf.Ticker(sym).funds_data
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
                symbols = [h.symbol for h in summary.holdings if h.symbol]
                weights = {
                    h.symbol: float(h.market_value) / initial_value
                    for h in summary.holdings
                    if h.symbol and float(h.market_value) > 0
                }
                if symbols:
                    regime, _ = MarkovRegimeService().get_portfolio_and_ticker_regimes(
                        symbols=symbols, weights=weights
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
        target_survival: float = 85.0,
        max_rate: float = 20.0,
    ) -> "MaxSpendingResult":
        """Find the highest withdrawal rate keeping portfolio survival ≥ target.

        Survival is monotonically decreasing in the withdrawal rate, so we binary-
        search the rate (reusing the same simulation engine, regime, and SSA/age
        scenario as the displayed run) for the crossing point.
        """
        from app.schemas.analysis import MaxSpendingResult

        summary = self.portfolio_svc.get_summary()
        initial_value = float(summary.total_market_value) or 1.0
        regime = self._resolve_portfolio_regime(summary, initial_value, regime_override)
        current_alloc = {k: float(v) / 100 for k, v in summary.allocation.items()}

        if current_age is not None and retirement_age is not None:
            years = max(0, int(retirement_age) - int(current_age))

        ssa_annual = 0.0
        ssa_factor: float | None = None
        if (ssa_monthly_fra and float(ssa_monthly_fra) > 0
                and ssa_claiming_age is not None and retirement_age is not None):
            ssa_factor = self._ssa_claiming_factor(int(ssa_claiming_age))
            ssa_annual = float(ssa_monthly_fra) * 12.0 * ssa_factor

        wy = int(withdrawal_years)
        ca = int(current_age) if current_age is not None else None
        ra = int(retirement_age) if retirement_age is not None else None
        sca = int(ssa_claiming_age) if ssa_claiming_age is not None else None
        target = float(target_survival)

        def _sim_at(rate: float):
            return self._simulate(
                label="solve", allocation=current_alloc, initial_value=initial_value,
                years=years, num_simulations=num_simulations, goal_amount=None,
                regime=regime, monthly_contribution=float(monthly_contribution),
                withdrawal_rate=rate, withdrawal_years=wy,
                spending_pattern=spending_pattern,
                smile_slow_pct=smile_slow_pct, smile_no_pct=smile_no_pct,
                current_age=ca, retirement_age=ra, ssa_claiming_age=sca,
                ssa_annual_benefit=ssa_annual, ssa_fra_factor=ssa_factor,
            )

        def _survival(rate: float) -> float:
            r = _sim_at(rate)
            return float(r.portfolio_survival_rate or 0.0)

        # Binary search for the highest rate whose survival is still ≥ target.
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

        r = _sim_at(best)
        annual = r.withdrawal_annual_amount or Decimal("0")
        return MaxSpendingResult(
            target_survival=Decimal(str(round(target, 1))),
            max_withdrawal_rate=Decimal(str(round(best, 2))),
            annual_amount=annual,
            monthly_amount=Decimal(str(round(float(annual) / 12.0, 2))),
            achieved_survival=r.portfolio_survival_rate or Decimal("0"),
            ssa_annual_benefit=r.ssa_annual_benefit,
            withdrawal_years=wy,
        )

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
        summary = self.portfolio_svc.get_summary()
        initial_value = float(summary.total_market_value)
        if initial_value <= 0:
            initial_value = 1.0

        portfolio_regime = self._resolve_portfolio_regime(summary, initial_value, regime_override)

        # Current allocation weights
        current_alloc = {k: float(v) / 100 for k, v in summary.allocation.items()}

        monthly_contrib_float  = float(monthly_contribution)
        withdrawal_rate_float  = float(withdrawal_rate)
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
        earner has also claimed); the survivor keeps the larger of the two.

        Returns (annual $ per distribution year, summary). The summary is None
        when there's no spouse, so the single-stream behavior is unchanged.
        """
        def _dec(x) -> Decimal:
            return Decimal(str(round(float(x), 2)))

        primary_on = (primary_monthly_fra > 0 and primary_claiming_age is not None
                      and retirement_age is not None and withdrawal_years > 0)
        spouse_on = (spouse_monthly_fra > 0 and spouse_claiming_age is not None
                     and spouse_current_age is not None and retirement_age is not None
                     and withdrawal_years > 0)
        if not primary_on and not spouse_on:
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
            }

        pri = (_person(primary_monthly_fra, primary_claiming_age, primary_fra_age,
                       lambda y: retirement_age + y) if primary_on else None)
        spo = (_person(spouse_monthly_fra, spouse_claiming_age, spouse_fra_age,
                       lambda y: spouse_current_age + accumulation_years + y) if spouse_on else None)

        # Spousal top-up for the lower-FRA earner (only when both participate).
        topup_monthly, topup_target = 0.0, None
        if pri and spo:
            higher, lower, topup_target = (
                (pri, spo, "spouse") if pri["monthly_fra"] >= spo["monthly_fra"]
                else (spo, pri, "primary"))
            spousal_entitlement = 0.5 * higher["monthly_fra"] * self._ssa_spousal_factor(
                lower["claiming_age"], lower["fra_age"])
            topup_monthly = max(0.0, spousal_entitlement - lower["own_monthly"])

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
            )

        household = None
        if pri and spo:
            people = [_to_person(pri, "You", topup_target == "primary"),
                      _to_person(spo, "Spouse", topup_target == "spouse")]
            survivor_monthly = max(pri["own_monthly"], spo["own_monthly"])
            first = next((y for y in range(withdrawal_years) if ssa_per_year[y] > 0), None)
            household = SsaHousehold(
                people=people,
                combined_annual_benefit=_dec(max(ssa_per_year) if withdrawal_years else 0.0),
                survivor_annual_benefit=_dec(survivor_monthly * 12.0),
                first_benefit_plan_year=(first + 1) if first is not None else None,
            )
        return ssa_per_year, household

    def _run_income_bridge(
        self,
        *,
        final_vals,                       # (num_sims,) accumulation-ending values
        annual_wdraw,                     # (num_sims,) Go-Go after-tax spend per sim
        smile_mults,                      # (withdrawal_years,) spending-smile multipliers
        ssa_per_year,                     # (withdrawal_years,) guaranteed income per year
        dist_gf,                          # (num_sims, withdrawal_years) growth factors
        bucket_balances: dict[str, float],
        deferred_tax_rate: float,
        taxable_tax_rate: float,
        withdrawal_years: int,
        num_simulations: int,
        retirement_age: int | None,
        ssa_claiming_age: int | None,
    ) -> "tuple[np.ndarray, IncomeBridge]":
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
        path produces, so the fan chart and summary stats are unchanged) plus an
        IncomeBridge summary of medians answering "which accounts fund the
        pre-SSA years".
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

        dist_values = np.empty((num_simulations, withdrawal_years), dtype=float)
        taxes_per_sim = np.zeros(num_simulations, dtype=float)
        funding_by_year: list[BridgeYearFunding] = []
        depletes_year: dict[str, int | None] = {b: None for b in order}

        for y in range(withdrawal_years):
            # Grow every bucket, then meet the year's net spending need in order.
            for b in order:
                buckets[b] = buckets[b] * dist_gf[:, y]

            spending_need = annual_wdraw * smile_mults[y]                    # after-tax $ wanted
            need = np.maximum(spending_need - ssa_per_year[y], 0.0)          # left after SSA

            gross_by_src: dict[str, "np.ndarray"] = {}
            for b in order:
                net_factor = 1.0 - rates[b]
                gross_needed = need / net_factor if net_factor > 0 else need
                gross_taken = np.minimum(buckets[b], gross_needed)
                net_delivered = gross_taken * net_factor
                buckets[b] = buckets[b] - gross_taken
                taxes_per_sim += gross_taken - net_delivered
                need = need - net_delivered
                gross_by_src[b] = gross_taken

            dist_values[:, y] = buckets["taxable"] + buckets["traditional"] + buckets["roth"]

            age = (retirement_age + y) if retirement_age is not None else None
            funding_by_year.append(BridgeYearFunding(
                year=y + 1,
                age=age,
                ssa=_d(ssa_per_year[y]),
                taxable=_d(np.median(gross_by_src["taxable"])),
                traditional=_d(np.median(gross_by_src["traditional"])),
                roth=_d(np.median(gross_by_src["roth"])),
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
        gap_annual = (float(np.median(np.maximum(
            annual_wdraw * smile_mults[0] - ssa_per_year[0], 0.0)))
            if withdrawal_years else 0.0)

        # Total gross capital to fund the pre-SSA window = sum of median gross
        # draws (all sources) across the bridge years.
        bridge_number = sum(
            float(r.taxable) + float(r.traditional) + float(r.roth)
            for r in funding_by_year[:bridge_years]
        )
        bridge_coverage = (float(np.mean(dist_values[:, bridge_years - 1] > 0) * 100.0)
                           if bridge_years > 0 else 100.0)

        buckets_out: list[BridgeBucket] = []
        for b in order:
            dy = depletes_year[b]
            dage = (retirement_age + dy - 1) if (dy is not None and retirement_age is not None) else None
            buckets_out.append(BridgeBucket(
                bucket=b,
                label=labels[b],
                start_balance=_d(props[b] * float(np.median(final_vals))),
                end_median=_d(np.median(buckets[b])),
                depletes_year=dy,
                depletes_age=dage,
            ))

        bridge = IncomeBridge(
            enabled=True,
            buckets=buckets_out,
            total_start=_d(np.median(final_vals)),
            gap_annual=_d(gap_annual),
            bridge_years=bridge_years,
            bridge_number=_d(bridge_number),
            bridge_coverage_pct=_d(bridge_coverage),
            deferred_tax_rate=_d(rates["traditional"] * 100.0),
            taxable_tax_rate=_d(rates["taxable"] * 100.0),
            taxes_total_median=_d(np.median(taxes_per_sim)),
            funding_by_year=funding_by_year,
        )
        return dist_values, bridge

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
    ) -> MonteCarloResult:
        """
        Two-phase Monte Carlo: accumulation → distribution.

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
        # Build per-asset-class return/volatility vectors
        ac_weights = []
        ac_returns = []
        ac_vols = []

        for ac, weight in allocation.items():
            if weight <= 0:
                continue
            assumptions = ASSET_CLASS_ASSUMPTIONS.get(ac, ASSET_CLASS_ASSUMPTIONS["other"])
            ac_weights.append(weight)
            ac_returns.append(assumptions["return"])
            ac_vols.append(assumptions["volatility"])

        if not ac_weights:
            ac_weights = [1.0]
            ac_returns = [0.04]
            ac_vols = [0.05]

        w = np.array(ac_weights)
        w = w / w.sum()  # normalize
        mu = np.array(ac_returns)
        sigma = np.array(ac_vols)

        # Portfolio expected return and volatility (simplified: uncorrelated assumption)
        port_mu = float(w @ mu)
        port_sigma = float(np.sqrt((w ** 2) @ (sigma ** 2)))

        # Simulate: proper log-normal Geometric Brownian Motion with Ito correction.
        # drift = mu − 0.5 σ² ensures E[S_t] = S_0 · exp(mu · t)
        rng = np.random.default_rng(seed=42)
        drift = port_mu - 0.5 * (port_sigma ** 2)
        # Shape: (num_simulations, years)
        log_returns = rng.normal(drift, port_sigma, size=(num_simulations, years))

        # ── Regime conditioning — override first 2 years ──────────────────────
        # Blend schedule: year 0 = 100 % regime, year 1 = 50 %, year 2+ = base
        regime_context = "Base"
        if regime is not None:
            regime_context = regime.current_regime
            REGIME_ADJ = {
                "Bull":     {"mu_delta": +0.02, "sigma_factor": 0.90},
                "Sideways": {"mu_delta":  0.00, "sigma_factor": 1.00},
                "Bear":     {"mu_delta": -0.03, "sigma_factor": 1.20},
            }
            adj = REGIME_ADJ.get(regime.current_regime, {"mu_delta": 0.0, "sigma_factor": 1.0})
            blend_schedule = [1.0, 0.5]  # only override years 0 and 1
            for y, blend in enumerate(blend_schedule[:years]):
                if blend == 0:
                    continue
                adj_mu     = port_mu + blend * adj["mu_delta"]
                adj_sigma  = port_sigma * (1.0 + blend * (adj["sigma_factor"] - 1.0))
                adj_drift  = adj_mu - 0.5 * adj_sigma ** 2
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
                vals = cumulative[:, y]
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
        if goal_amount:
            prob_goal = float(np.mean(final_vals >= goal_amount) * 100)

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

        if withdrawal_rate > 0 and withdrawal_years > 0:
            # Per-simulation annual withdrawal: constant dollar amount based on
            # each simulation's own ending accumulation value (Go-Go rate).
            annual_wdraw = final_vals * (withdrawal_rate / 100.0)  # shape: (num_sims,)

            # ── Spending Smile: build per-year multipliers ────────────────────
            # Split distribution years into thirds: Go-Go (1.0×) → Slow-Go → No-Go
            import math as _math
            smile_mults = np.ones(withdrawal_years)
            go_go_yrs = slow_go_yrs = 0
            if spending_pattern == "smile" and withdrawal_years > 0:
                go_go_yrs   = _math.ceil(withdrawal_years / 3)
                slow_go_yrs = _math.ceil(withdrawal_years / 3)
                # Guard: go-go + slow-go must not consume all years
                if go_go_yrs + slow_go_yrs >= withdrawal_years:
                    slow_go_yrs = max(0, withdrawal_years - go_go_yrs)
                for y in range(withdrawal_years):
                    if y < go_go_yrs:
                        smile_mults[y] = 1.0
                    elif y < go_go_yrs + slow_go_yrs:
                        smile_mults[y] = smile_slow_pct / 100.0
                    else:
                        smile_mults[y] = smile_no_pct / 100.0

            # Fresh base-case growth factors for the distribution phase
            # (no regime conditioning — we're past the 2-year regime window)
            dist_drift = port_mu - 0.5 * (port_sigma ** 2)
            dist_log_rets = rng.normal(dist_drift, port_sigma,
                                       size=(num_simulations, withdrawal_years))
            dist_gf = np.exp(dist_log_rets)

            # ── Social Security income offset ──────────────────────────────────
            # Before any benefit starts the portfolio funds 100% of spending (the
            # "income bridge"); once SSA is flowing it covers part of each year's
            # need and the portfolio only draws the remaining gap. The per-year
            # schedule is supplied by run_monte_carlo (one or two earners); the
            # scalar fallback keeps direct _simulate callers (and tests) working.
            ssa_per_year = np.zeros(withdrawal_years)
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
                dist_values, income_bridge_out = self._run_income_bridge(
                    final_vals=final_vals,
                    annual_wdraw=annual_wdraw,
                    smile_mults=smile_mults,
                    ssa_per_year=ssa_per_year,
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
                for y in range(withdrawal_years):
                    # End-of-year: portfolio grows first; the smile-adjusted spending
                    # need is met from SSA first, then the portfolio covers the rest.
                    spending_need  = annual_wdraw * smile_mults[y]                  # per-sim ($)
                    portfolio_draw = np.maximum(spending_need - ssa_per_year[y], 0.0)
                    port_d = np.maximum(port_d * dist_gf[:, y] - portfolio_draw, 0.0)
                    dist_values[:, y] = port_d

            # First point: retirement starting value = end of accumulation.
            # This anchors the orange distribution fan to the exact point where
            # the blue accumulation fan ends, so the chart shows a clear kink
            # when contributions stop and withdrawals begin.
            withdrawal_trajectories.append(SimulationOutcome(
                year=years,  # same index as last accumulation year
                p10=Decimal(str(round(float(np.percentile(final_vals, 10)), 2))),
                p25=Decimal(str(round(float(np.percentile(final_vals, 25)), 2))),
                median=Decimal(str(round(float(np.median(final_vals)), 2))),
                p75=Decimal(str(round(float(np.percentile(final_vals, 75)), 2))),
                p90=Decimal(str(round(float(np.percentile(final_vals, 90)), 2))),
            ))

            for y in range(withdrawal_years):
                vals_d = dist_values[:, y]
                withdrawal_trajectories.append(SimulationOutcome(
                    year=years + y + 1,   # continues year-count from accumulation
                    p10=Decimal(str(round(float(np.percentile(vals_d, 10)), 2))),
                    p25=Decimal(str(round(float(np.percentile(vals_d, 25)), 2))),
                    median=Decimal(str(round(float(np.median(vals_d)), 2))),
                    p75=Decimal(str(round(float(np.percentile(vals_d, 75)), 2))),
                    p90=Decimal(str(round(float(np.percentile(vals_d, 90)), 2))),
                ))

            med_wdraw = float(np.median(annual_wdraw))
            final_d   = dist_values[:, -1]
            survival  = float(np.mean(final_d > 0) * 100)

            # Total withdrawn = Go-Go rate × sum of smile multipliers
            # (accounts for step-downs in Slow-Go and No-Go phases)
            total_distributed = med_wdraw * float(np.sum(smile_mults))

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
            final_median=Decimal(str(round(float(np.median(final_vals)), 2))),
            final_p10=Decimal(str(round(float(np.percentile(final_vals, 10)), 2))),
            final_p90=Decimal(str(round(float(np.percentile(final_vals, 90)), 2))),
            probability_of_goal=Decimal(str(round(prob_goal, 1))) if prob_goal is not None else None,
            goal_amount=Decimal(str(goal_amount)) if goal_amount else None,
            regime_context=regime_context,
            monthly_contribution=Decimal(str(round(monthly_contribution, 2))),
            withdrawal_rate=Decimal(str(round(withdrawal_rate, 2))),
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
        Compute pairwise Pearson correlation of daily log-returns for all
        holdings that have price history.  Private / illiquid positions without
        sufficient history are excluded so the matrix remains clean.
        """
        summary = self.portfolio_svc.get_summary()
        end_date = date.today()
        start_date = end_date - timedelta(days=lookback_days)

        symbols = [h.symbol for h in summary.holdings if h.symbol]
        bulk = self._bulk_daily_returns(symbols, start_date, end_date)

        # Keep only symbols with enough data
        good: list[tuple[str, np.ndarray]] = [
            (sym, ret) for sym, ret in bulk.items()
            if ret is not None and len(ret) >= 30
        ]
        if len(good) < 2:
            return CorrelationMatrix(
                symbols=[s for s, _ in good],
                matrix=[],
                lookback_days=lookback_days,
                as_of=end_date,
            )

        good.sort(key=lambda x: x[0])
        symbol_list = [s for s, _ in good]
        min_len = min(len(r) for _, r in good)
        mat = np.column_stack([r[-min_len:] for _, r in good])
        corr = np.corrcoef(mat.T)

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
        Compute Time-Weighted Return (TWR) and approximate Money-Weighted Return
        (XIRR / IRR) for the whole portfolio over multiple trailing windows.

        TWR methodology: chain-link sub-period returns between cash-flow dates
        using daily portfolio values derived from stored price history.

        For portfolios without dense price history, we fall back to computing
        geometric cumulative returns from the daily portfolio return series used
        by the risk engine.
        """
        summary = self.portfolio_svc.get_summary()
        total_mv = float(summary.total_market_value)
        if total_mv <= 0:
            return ReturnMetrics()

        end_date = date.today()
        windows = {
            "1yr": 252,
            "3yr": 756,
            "5yr": 1260,
        }

        # Fetch bulk returns
        symbols = [h.symbol for h in summary.holdings if h.symbol]
        all_returns = self._bulk_daily_returns(symbols, end_date - timedelta(days=1300), end_date)

        weights: list[float] = []
        return_series: list[np.ndarray] = []
        for h in summary.holdings:
            w = float(h.market_value) / total_mv
            rets = all_returns.get(h.symbol) if h.symbol else None
            if rets is not None and len(rets) >= 30:
                weights.append(w)
                return_series.append(rets)

        if not return_series:
            return ReturnMetrics()

        min_len = min(len(r) for r in return_series)
        aligned = np.column_stack([r[-min_len:] for r in return_series])
        w_arr = np.array(weights)
        w_arr = w_arr / w_arr.sum()
        port_daily = aligned @ w_arr  # daily portfolio returns

        def _twr(n_days: int) -> Decimal | None:
            if len(port_daily) < n_days:
                return None
            sub = port_daily[-n_days:]
            # Chain-link: product of (1 + r_t) minus 1, annualised
            cumulative = float(np.prod(1 + sub)) - 1
            years = n_days / 252
            annualised = (1 + cumulative) ** (1 / years) - 1
            return Decimal(str(round(annualised * 100, 2)))

        inception_twr = None
        if len(port_daily) > 0:
            cumulative_all = float(np.prod(1 + port_daily)) - 1
            years_all = len(port_daily) / 252
            if years_all > 0:
                inception_ann = (1 + cumulative_all) ** (1 / years_all) - 1
                inception_twr = Decimal(str(round(inception_ann * 100, 2)))

        inception_date = end_date - timedelta(days=len(port_daily))

        return ReturnMetrics(
            twr_1yr=_twr(252),
            twr_3yr=_twr(756),
            twr_5yr=_twr(1260),
            twr_inception=inception_twr,
            annualized_since=inception_date,
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

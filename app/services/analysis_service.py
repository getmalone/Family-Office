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
    ConcentrationRisk,
    CorrelationMatrix,
    DriftAnalysis,
    InvestmentProfileSchema,
    MonteCarloComparison,
    MonteCarloResult,
    PositionRisk,
    RebalanceAction,
    RegimeState,
    ReturnMetrics,
    RiskMetrics,
    SimulationOutcome,
)
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService


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

        concentration = ConcentrationRisk(
            top_5_weight_pct=Decimal(str(round(top5, 2))),
            top_10_weight_pct=Decimal(str(round(top10, 2))),
            largest_position=largest.symbol or largest.name,
            largest_position_weight_pct=largest.weight_pct,
            hhi_index=Decimal(str(round(hhi, 2))),
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
        # (e.g. for a 252-day window, require ≥150 rows).  Anything shorter means
        # the DB is still being built and yfinance will give far better data.
        calendar_days = max(1, (end_date - start_date).days)
        min_rows_required = max(30, int(calendar_days * 0.60))

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

        # Second: bulk-download remaining symbols from yfinance
        missing = [s for s in symbols if s not in results]
        if missing:
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
            description=p.description,
            target_allocations={k: Decimal(v) for k, v in allocs.items()},
        )

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

        # ── Regime conditioning — manual override OR auto Markov detection ─────
        portfolio_regime: RegimeState | None = None

        if regime_override and regime_override.lower() != "base":
            # Manual regime selected by user — build a synthetic RegimeState
            from app.schemas.analysis import RegimeTransition
            _rt = RegimeTransition(bull=0.0, sideways=0.0, bear=0.0)
            _name = regime_override.capitalize()  # "bull"→"Bull" etc.
            portfolio_regime = RegimeState(
                symbol="Manual Override",
                current_regime=_name,
                persist_pct=0.0,
                one_step=_rt, five_step=_rt, stationary=_rt,
                signal_score=0.0,
                lookback_days=0,
            )
        elif not regime_override:
            # Auto-detect: portfolio-blended Markov (never blocks on failure)
            try:
                from app.services.markov_regime_service import MarkovRegimeService
                symbols_with_data = [h.symbol for h in summary.holdings if h.symbol]
                weights_map = {
                    h.symbol: float(h.market_value) / initial_value
                    for h in summary.holdings
                    if h.symbol and float(h.market_value) > 0
                }
                if symbols_with_data:
                    portfolio_regime, _ = MarkovRegimeService().get_portfolio_and_ticker_regimes(
                        symbols=symbols_with_data,
                        weights=weights_map,
                    )
            except Exception:
                pass
        # else: regime_override == "base" → portfolio_regime stays None (no conditioning)

        # Current allocation weights
        current_alloc = {k: float(v) / 100 for k, v in summary.allocation.items()}

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
                )

        return MonteCarloComparison(
            current=current_result,
            target=target_result,
            goal_amount=goal_amount,
        )

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

            port_d = final_vals.copy()
            dist_values = np.empty((num_simulations, withdrawal_years), dtype=float)
            for y in range(withdrawal_years):
                # End-of-year: portfolio grows first, then withdraw smile-adjusted amount
                effective_wdraw = annual_wdraw * smile_mults[y]
                port_d = np.maximum(port_d * dist_gf[:, y] - effective_wdraw, 0.0)
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
                    is_active=(profile_data["risk_tolerance"] == "balanced"),  # Default active
                    description=profile_data["description"],
                    target_allocations_json=json.dumps(
                        {k: str(v) for k, v in profile_data["allocations"].items()}
                    ),
                )
                self.session.add(p)
        self.session.flush()

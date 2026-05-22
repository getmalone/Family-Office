"""
Financial analysis Pydantic schemas for the Family Office.

Covers risk metrics, investment profiles, drift/rebalancing analysis,
and Monte Carlo simulation results.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


# --- Risk Analysis ---

class PositionRisk(BaseModel):
    asset_id: int
    symbol: str | None
    name: str
    asset_class: str
    weight_pct: Decimal
    annualized_volatility: Decimal | None
    beta: Decimal | None
    risk_contribution_pct: Decimal | None


class ConcentrationRisk(BaseModel):
    top_5_weight_pct: Decimal
    top_10_weight_pct: Decimal
    largest_position: str
    largest_position_weight_pct: Decimal
    hhi_index: Decimal


class AssetClassRisk(BaseModel):
    asset_class: str
    weight_pct: Decimal
    annualized_volatility: Decimal | None
    contribution_to_risk: Decimal | None


class RiskMetrics(BaseModel):
    portfolio_volatility: Decimal
    sharpe_ratio: Decimal
    max_drawdown: Decimal
    max_drawdown_period: str
    beta: Decimal
    risk_free_rate: Decimal
    lookback_days: int
    # Value-at-Risk and Conditional VaR (Expected Shortfall) at 95% and 99% confidence
    var_95: Decimal | None = None   # 1-day VaR (dollar loss not exceeded 95% of the time)
    cvar_95: Decimal | None = None  # Expected loss given loss > VaR_95 (Expected Shortfall)
    var_99: Decimal | None = None
    cvar_99: Decimal | None = None
    position_risks: list[PositionRisk]
    concentration: ConcentrationRisk
    asset_class_risks: list[AssetClassRisk]


# --- Markov Regime Detection ---

class RegimeTransition(BaseModel):
    """One probability row: P(Bull), P(Sideways), P(Bear) summing to ~1."""
    bull: float
    sideways: float
    bear: float


class RegimeState(BaseModel):
    """Full Markov chain regime snapshot for a single symbol."""
    symbol: str
    current_regime: str          # "Bull" | "Sideways" | "Bear"
    persist_pct: float           # % chance of staying in current regime (diagonal of T)
    one_step: RegimeTransition   # T^1 distribution from current state
    five_step: RegimeTransition  # T^5 distribution from current state
    stationary: RegimeTransition # Long-run equilibrium distribution
    signal_score: float          # P(Bull) - P(Bear) from stationary dist, −1.0 to +1.0
    lookback_days: int
    as_of: date | None = None


# --- Correlation & Returns ---

class CorrelationEntry(BaseModel):
    symbol_a: str
    symbol_b: str
    correlation: Decimal


class CorrelationMatrix(BaseModel):
    symbols: list[str]
    matrix: list[list[Decimal]]  # N×N, indexed by symbols order
    lookback_days: int
    as_of: date


class ReturnMetrics(BaseModel):
    """Time-weighted and money-weighted return metrics."""
    twr_1yr: Decimal | None = None   # 1-year time-weighted return %
    twr_3yr: Decimal | None = None
    twr_5yr: Decimal | None = None
    twr_inception: Decimal | None = None
    mwr_1yr: Decimal | None = None   # 1-year money-weighted return (XIRR) %
    annualized_since: date | None = None  # earliest data date used for inception TWR


# --- Investment Profiles ---

class InvestmentProfileSchema(BaseModel):
    id: int
    name: str
    risk_tolerance: str
    is_system: bool
    is_active: bool
    description: str | None
    target_allocations: dict[str, Decimal]

    model_config = {"from_attributes": True}


class ProfileCreate(BaseModel):
    name: str
    description: str = ""
    target_allocations: dict[str, Decimal]


# --- Drift & Rebalancing ---

class AllocationTarget(BaseModel):
    asset_class: str
    target_pct: Decimal
    current_pct: Decimal
    drift_pct: Decimal
    drift_status: str  # on_target, overweight, underweight


class RebalanceAction(BaseModel):
    asset_class: str
    action: str  # buy or sell
    target_change_pct: Decimal
    estimated_dollar_amount: Decimal
    suggested_symbol: str | None


class DriftAnalysis(BaseModel):
    profile_name: str
    risk_tolerance: str
    targets: list[AllocationTarget]
    rebalance_actions: list[RebalanceAction]
    total_rebalance_value: Decimal


# --- Monte Carlo ---

class SimulationOutcome(BaseModel):
    year: int
    p10: Decimal
    p25: Decimal
    median: Decimal
    p75: Decimal
    p90: Decimal


class MonteCarloResult(BaseModel):
    label: str
    initial_value: Decimal
    num_simulations: int
    years: int
    trajectories: list[SimulationOutcome]
    final_median: Decimal
    final_p10: Decimal
    final_p90: Decimal
    probability_of_goal: Decimal | None = None
    goal_amount: Decimal | None = None
    regime_context: str = "Base"                    # "Bull" | "Sideways" | "Bear" | "Base"
    monthly_contribution: Decimal = Decimal("0")    # regular monthly deposit during accumulation

    # ── Distribution / withdrawal phase ──────────────────────────────────────
    withdrawal_rate: Decimal = Decimal("0")         # annual % of retirement-date portfolio
    withdrawal_years: int = 0                       # how many years the distribution phase runs
    withdrawal_trajectories: list["SimulationOutcome"] = []  # per-year in distribution phase
    withdrawal_annual_amount: Decimal | None = None  # median Go-Go annual withdrawal (initial rate)
    withdrawal_total: Decimal | None = None          # median total withdrawn (smile-adjusted)
    withdrawal_final_median: Decimal | None = None   # median portfolio at end of distribution
    withdrawal_final_p10: Decimal | None = None
    withdrawal_final_p90: Decimal | None = None
    portfolio_survival_rate: Decimal | None = None   # % of sims where portfolio > 0 at end

    # ── Spending Smile ────────────────────────────────────────────────────────
    spending_pattern: str = "constant"              # "constant" | "smile"
    smile_slow_pct: float | None = None             # Slow-Go spending as % of Go-Go rate
    smile_no_pct: float | None = None               # No-Go spending as % of Go-Go rate
    smile_go_go_years: int = 0                      # number of Go-Go years
    smile_slow_go_years: int = 0                    # number of Slow-Go years


class MonteCarloComparison(BaseModel):
    current: MonteCarloResult
    target: MonteCarloResult | None = None
    goal_amount: Decimal | None = None

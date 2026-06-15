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


class LookthroughPosition(BaseModel):
    """One underlying security after ETF/fund expansion."""
    symbol: str
    weight_pct: Decimal          # portfolio-level weight (0–100)
    via_fund: str | None = None  # fund it was expanded from, or None if direct holding


class ConcentrationRisk(BaseModel):
    top_5_weight_pct: Decimal
    top_10_weight_pct: Decimal
    largest_position: str
    largest_position_weight_pct: Decimal
    hhi_index: Decimal
    # Look-through HHI: ETFs/funds expanded to their underlying securities
    hhi_lookthrough: Decimal | None = None
    hhi_lookthrough_positions: int | None = None         # distinct underlying positions
    hhi_lookthrough_coverage_pct: Decimal | None = None  # % of portfolio MV expanded
    hhi_lookthrough_top: list[LookthroughPosition] = []  # top 15 underlying positions


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
    is_comparison_a: bool = False
    is_comparison_b: bool = False
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

    # ── Retirement timeline & Social Security ──────────────────────────────────
    # All None when ages aren't supplied (the simulation stays purely year-based).
    current_age: int | None = None                  # household planning age today
    retirement_age: int | None = None               # age accumulation ends / withdrawals begin
    ssa_claiming_age: int | None = None             # age Social Security benefits start (62–70)
    ssa_monthly_benefit: Decimal | None = None      # claiming-age-adjusted benefit, $/month
    ssa_annual_benefit: Decimal | None = None       # claiming-age-adjusted benefit, $/year
    ssa_fra_factor: float | None = None             # benefit as a fraction of the FRA amount (0.70, 1.24…)
    ssa_total_benefit: Decimal | None = None        # total SSA income received over the plan horizon
    bridge_years: int | None = None                 # retirement years before SSA (portfolio funds 100%)
    # Annual portfolio draw once SSA is flowing (median): spending need − SSA, floored at 0.
    net_draw_after_ssa: Decimal | None = None

    # ── Income Bridge: account-level withdrawal sequencing ─────────────────────
    # Populated only when the bridge is enabled and the portfolio has bucketed
    # balances; otherwise None and the model is the single pooled portfolio.
    income_bridge: "IncomeBridge | None" = None

    # ── Household Social Security (two earners) ────────────────────────────────
    # Populated when a spouse stream is supplied; otherwise the single ssa_*
    # fields above describe the one modeled benefit.
    ssa_household: "SsaHousehold | None" = None


class BridgeBucket(BaseModel):
    """One tax-treatment bucket in the Income Bridge withdrawal sequence."""
    bucket: str                         # "taxable" | "traditional" | "roth"
    label: str                          # display name, e.g. "Tax-Deferred"
    start_balance: Decimal              # bucket value at the start of retirement (median)
    end_median: Decimal                 # median bucket value at end of distribution
    depletes_year: int | None = None    # plan-year (1-based in distribution) the median bucket hits 0; None = survives
    depletes_age: int | None = None     # household age at depletion, if ages supplied


class BridgeYearFunding(BaseModel):
    """Median funding of one distribution year, by source (for the stacked view)."""
    year: int                           # 1-based year within the distribution phase
    age: int | None = None
    ssa: Decimal                        # guaranteed income applied this year
    taxable: Decimal                    # gross drawn from the taxable bucket
    traditional: Decimal                # gross drawn from the tax-deferred bucket
    roth: Decimal                       # gross drawn from the tax-free bucket


class IncomeBridge(BaseModel):
    """Account-level withdrawal sequencing answering 'which accounts fund the
    pre-SSA years' — taxable → traditional → Roth, with a simple effective-tax
    gross-up. All figures are medians across the simulation set."""
    enabled: bool = True
    buckets: list[BridgeBucket] = []
    total_start: Decimal = Decimal("0")            # taxable+traditional+roth at retirement
    # Gap & Bridge numbers
    gap_annual: Decimal = Decimal("0")             # annual after-tax spend the portfolio covers during the bridge
    bridge_years: int = 0                          # retirement years before SSA starts
    bridge_number: Decimal = Decimal("0")          # total gross capital needed to fund the pre-SSA window
    bridge_coverage_pct: Decimal = Decimal("0")    # % of sims that fund the full bridge without depleting
    # Tax assumptions / outcome
    deferred_tax_rate: Decimal = Decimal("0")      # effective rate on tax-deferred withdrawals (%)
    taxable_tax_rate: Decimal = Decimal("0")       # effective cap-gains drag on taxable withdrawals (%)
    taxes_total_median: Decimal = Decimal("0")     # median lifetime tax paid on portfolio withdrawals
    funding_by_year: list[BridgeYearFunding] = []


class SsaPerson(BaseModel):
    """One earner's Social Security stream within a household."""
    label: str                                       # "You" / "Spouse" (or a name)
    monthly_fra: Decimal                             # entered benefit at this person's FRA
    fra_age: int                                     # full retirement age used (66–67)
    claiming_age: int                                # age this person claims (62–70)
    fra_factor: float                                # claiming adjustment on their own record
    own_monthly_benefit: Decimal                     # own benefit after the claiming factor
    spousal_monthly_benefit: Decimal = Decimal("0")  # spousal top-up applied (lower earner only)
    monthly_benefit: Decimal                         # total monthly once flowing (own + spousal top-up)
    annual_benefit: Decimal
    starts_plan_year: int | None = None              # distribution-year index their benefit starts (None = before retirement)


class SsaHousehold(BaseModel):
    """Two-earner Social Security with spousal and survivor benefits."""
    people: list[SsaPerson] = []
    combined_annual_benefit: Decimal = Decimal("0")  # household total once both streams are flowing
    survivor_annual_benefit: Decimal = Decimal("0")  # what the survivor keeps (the larger single benefit)
    first_benefit_plan_year: int | None = None       # distribution-year index the first SSA dollar arrives


class MaxSpendingResult(BaseModel):
    """Highest sustainable spending for a target portfolio survival rate."""
    target_survival: Decimal                 # e.g. 85 (%)
    max_withdrawal_rate: Decimal             # % of retirement-date portfolio
    annual_amount: Decimal                   # median Go-Go annual spend at that rate
    monthly_amount: Decimal
    achieved_survival: Decimal               # survival at the solved rate (≈ target)
    ssa_annual_benefit: Decimal | None = None
    withdrawal_years: int = 0


class MonteCarloComparison(BaseModel):
    current: MonteCarloResult
    target: MonteCarloResult | None = None
    comparison_a: MonteCarloResult | None = None
    comparison_b: MonteCarloResult | None = None
    goal_amount: Decimal | None = None


# Resolve the forward reference to IncomeBridge (defined after MonteCarloResult).
MonteCarloResult.model_rebuild()

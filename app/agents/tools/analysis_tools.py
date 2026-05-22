"""
Analysis Agent tools — LangChain tool wrappers around AnalysisService.

These tools enable the Analysis Agent to compute risk metrics, manage
investment profiles, analyze allocation drift, and run Monte Carlo simulations.
"""

import json
from decimal import Decimal

from langchain_core.tools import tool


def create_analysis_tools(session_factory):
    """Create analysis tools with injected session factory."""

    @tool
    def get_risk_metrics(lookback_days: int = 252) -> str:
        """Get portfolio risk metrics: volatility, Sharpe ratio, max drawdown, beta, concentration."""
        from app.services.analysis_service import AnalysisService

        with session_factory() as session:
            svc = AnalysisService(session)
            metrics = svc.get_risk_metrics(lookback_days=lookback_days)
            return metrics.model_dump_json()

    @tool
    def get_allocation_drift(profile_id: int | None = None) -> str:
        """Get current vs target allocation drift and rebalancing recommendations."""
        from app.services.analysis_service import AnalysisService

        with session_factory() as session:
            svc = AnalysisService(session)
            drift = svc.get_drift_analysis(profile_id=profile_id)
            return drift.model_dump_json()

    @tool
    def list_investment_profiles() -> str:
        """List all investment profiles (system presets and custom)."""
        from app.services.analysis_service import AnalysisService

        with session_factory() as session:
            svc = AnalysisService(session)
            profiles = svc.list_profiles()
            return json.dumps([p.model_dump(mode="json") for p in profiles])

    @tool
    def set_investment_profile(profile_id: int) -> str:
        """Set the active investment profile for target allocation tracking."""
        from app.services.analysis_service import AnalysisService

        with session_factory() as session:
            svc = AnalysisService(session)
            profile = svc.set_active_profile(profile_id)
            session.commit()
            return f"Active profile set to: {profile.name} ({profile.risk_tolerance})"

    @tool
    def run_monte_carlo_simulation(
        years: int = 10, num_simulations: int = 1000, goal_amount: float | None = None
    ) -> str:
        """Run Monte Carlo simulation of portfolio trajectory. Returns percentile outcomes and probability of reaching goal."""
        from app.services.analysis_service import AnalysisService

        with session_factory() as session:
            svc = AnalysisService(session)
            result = svc.run_monte_carlo(
                years=years,
                num_simulations=num_simulations,
                goal_amount=Decimal(str(goal_amount)) if goal_amount else None,
            )
            return result.model_dump_json()

    return [
        get_risk_metrics,
        get_allocation_drift,
        list_investment_profiles,
        set_investment_profile,
        run_monte_carlo_simulation,
    ]

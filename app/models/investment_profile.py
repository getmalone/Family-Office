"""
Investment profile model for the Family Office.

Stores target asset allocation profiles (Conservative through Aggressive plus custom)
that drive drift analysis, rebalancing recommendations, and Monte Carlo comparisons.
"""

import enum

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RiskToleranceEnum(str, enum.Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    BALANCED = "balanced"
    GROWTH = "growth"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class InvestmentProfile(Base, TimestampMixin):
    """
    An investment profile with target asset class allocations.

    System profiles (is_system=True) represent the 5 standard risk levels.
    Custom profiles allow the Principal to define bespoke allocation targets.
    Only one profile can be active at a time (is_active=True).
    Target allocations are stored as a JSON string mapping asset class to percentage.
    """

    __tablename__ = "investment_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    risk_tolerance: Mapped[RiskToleranceEnum] = mapped_column(String(50))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_comparison_a: Mapped[bool] = mapped_column(Boolean, default=False)
    is_comparison_b: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target_allocations_json: Mapped[str] = mapped_column(String(2000))

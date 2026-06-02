"""Asset and price models for the Family Office."""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AssetClassEnum(str, enum.Enum):
    US_EQUITY = "us_equity"
    INTL_EQUITY = "intl_equity"
    FIXED_INCOME = "fixed_income"
    REAL_ESTATE = "real_estate"
    PRIVATE_EQUITY = "private_equity"
    VENTURE_CAPITAL = "venture_capital"
    HEDGE_FUND = "hedge_fund"
    COMMODITY = "commodity"
    CRYPTO = "crypto"
    CASH = "cash"
    ALTERNATIVE = "alternative"
    OTHER = "other"


class Asset(Base, TimestampMixin):
    """An investable asset tracked by the Family Office."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    asset_class: Mapped[AssetClassEnum] = mapped_column(String(50))
    cusip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_publicly_traded: Mapped[bool] = mapped_column(Boolean, default=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_qsbs_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional public-market equivalent ticker used *only* for look-through HHI.
    # Set this on institutional / pooled-class funds that mirror a publicly-listed
    # fund (e.g. "Fidelity Contrafund Pool Cl F" → look_through_ticker="FCNTX").
    # Does NOT affect price fetching — only the concentration analysis.
    look_through_ticker: Mapped[str | None] = mapped_column(String(20), nullable=True)


class AssetPrice(Base):
    """Daily closing price cache for publicly traded assets."""

    __tablename__ = "asset_prices"
    __table_args__ = (UniqueConstraint("asset_id", "price_date", name="uq_asset_price_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    price_date: Mapped[date] = mapped_column(Date)
    close_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="yfinance")

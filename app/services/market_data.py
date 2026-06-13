"""
Market data service for the Family Office.

Fetches and caches asset prices from yfinance for the Portfolio Agent's
real-time NAV calculations and the Tax Optimization Agent's unrealized
gain/loss identification.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetPrice


# Symbols that are always $1.00 — never query yfinance for these.
# "CASH" on yfinance is the PGIM Ultra Short Bond ETF (~$86), not cash.
STABLE_VALUE_SYMBOLS = {
    "CASH", "SWTXX", "SWVXX", "VMFXX", "VMSXX", "FDRXX", "SPRXX",
    "FZFXX", "FDLXX", "FTEXX",  # common money-market fund tickers
}


class MarketDataService:
    """Fetches and caches market prices for publicly traded assets."""

    def __init__(self, session: Session, cache_ttl_minutes: int = 15):
        self.session = session
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)

    def get_current_price(self, asset: Asset) -> Decimal | None:
        """Get the most recent price for an asset, fetching from yfinance if stale."""
        if not asset.is_publicly_traded or not asset.symbol:
            return self._get_latest_manual_price(asset.id)

        cached = self._get_cached_price(asset.id)
        if cached is not None:
            return cached

        return self._fetch_and_cache(asset)

    def get_current_price_by_symbol(self, symbol: str) -> Decimal | None:
        """Look up asset by symbol and return current price."""
        asset = self.session.query(Asset).filter(Asset.symbol == symbol).first()
        if not asset:
            return None
        return self.get_current_price(asset)

    def bulk_update_prices(self, symbols: list[str] | None = None) -> dict[str, Decimal]:
        """Fetch current prices for multiple assets at once."""
        if symbols is None:
            assets = (
                self.session.query(Asset)
                .filter(Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
                .all()
            )
        else:
            assets = self.session.query(Asset).filter(Asset.symbol.in_(symbols)).all()

        # Always exclude stable-value symbols from yfinance bulk fetch
        assets = [a for a in assets if a.symbol.upper() not in STABLE_VALUE_SYMBOLS]

        results = {}
        for asset in assets:
            price = self._fetch_and_cache(asset)
            if price is not None and asset.symbol:
                results[asset.symbol] = price
        return results

    def get_price_history(
        self, asset_id: int, start_date: date, end_date: date | None = None
    ) -> list[AssetPrice]:
        """Get historical prices from the cache."""
        end_date = end_date or date.today()
        return (
            self.session.query(AssetPrice)
            .filter(
                AssetPrice.asset_id == asset_id,
                AssetPrice.price_date >= start_date,
                AssetPrice.price_date <= end_date,
            )
            .order_by(AssetPrice.price_date)
            .all()
        )

    def _get_cached_price(self, asset_id: int, max_age_days: int = 3) -> Decimal | None:
        """Return the most recent cached price within max_age_days (default 3)."""
        cutoff = date.today() - timedelta(days=max_age_days)
        price = (
            self.session.query(AssetPrice)
            .filter(
                AssetPrice.asset_id == asset_id,
                AssetPrice.price_date >= cutoff,
            )
            .order_by(AssetPrice.price_date.desc())
            .first()
        )
        return price.close_price if price else None

    def _get_latest_manual_price(self, asset_id: int) -> Decimal | None:
        """Get the most recent manually entered price for private assets."""
        price = (
            self.session.query(AssetPrice)
            .filter(AssetPrice.asset_id == asset_id)
            .order_by(AssetPrice.price_date.desc())
            .first()
        )
        return price.close_price if price else None

    def _fetch_and_cache(self, asset: Asset) -> Decimal | None:
        """Fetch price from yfinance and store in cache."""
        if not asset.symbol:
            return None

        # Never hit yfinance for stable-value / cash-equivalent symbols —
        # "CASH" is a real ETF on yfinance (~$86) and would corrupt balances.
        if asset.symbol.upper() in STABLE_VALUE_SYMBOLS:
            return self._get_latest_manual_price(asset.id) or Decimal("1")

        try:
            import yfinance as yf

            ticker = yf.Ticker(asset.symbol)
            hist = ticker.history(period="2d")
            if hist.empty:
                return None

            row = hist.iloc[-1]
            price = Decimal(str(round(row["Close"], 6)))
            today = date.today()

            # Persist the fresh quote through a short-lived, immediately-committed
            # session rather than the request-scoped one. This keeps the SQLite
            # write lock held only for the duration of this tiny write instead of
            # for the whole request — so concurrent page loads (multiple devices
            # on the network) don't deadlock — and a write failure can never
            # leave the caller's session in a poisoned/rolled-back state.
            self._persist_price(asset.id, today, price, row)
            return price

        except Exception:
            # Fallback to latest cached price
            return self._get_latest_manual_price(asset.id) if asset.id else None

    def _persist_price(self, asset_id: int, price_date: date, price: Decimal, row) -> None:
        """Upsert a daily price in its own committed transaction.

        Failures (e.g. a transient ``database is locked`` under heavy concurrent
        load) are swallowed: the live price is still returned to the caller, it
        just isn't cached on this pass.
        """
        from app.services.db import get_factory

        try:
            with get_factory()() as wsession:
                existing = (
                    wsession.query(AssetPrice)
                    .filter(
                        AssetPrice.asset_id == asset_id,
                        AssetPrice.price_date == price_date,
                    )
                    .first()
                )
                high = Decimal(str(round(row.get("High", 0), 6)))
                low = Decimal(str(round(row.get("Low", 0), 6)))
                volume = int(row.get("Volume", 0))
                if existing:
                    existing.close_price = price
                    existing.high_price = high
                    existing.low_price = low
                    existing.volume = volume
                else:
                    wsession.add(
                        AssetPrice(
                            asset_id=asset_id,
                            price_date=price_date,
                            close_price=price,
                            high_price=high,
                            low_price=low,
                            volume=volume,
                            source="yfinance",
                        )
                    )
                wsession.commit()
        except Exception:
            pass

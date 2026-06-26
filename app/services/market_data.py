"""
Market data service for the Family Office.

Fetches and caches asset prices from yfinance for the Portfolio Agent's
real-time NAV calculations and the Tax Optimization Agent's unrealized
gain/loss identification.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.tax_lot import TaxLot


# Symbols that are always $1.00 — never query yfinance for these.
# "CASH" on yfinance is the PGIM Ultra Short Bond ETF (~$86), not cash.
STABLE_VALUE_SYMBOLS = {
    "CASH", "SWTXX", "SWVXX", "VMFXX", "VMSXX", "FDRXX", "SPRXX",
    "FZFXX", "FDLXX", "FTEXX",  # common money-market fund tickers
}


def is_cash_asset(asset) -> bool:
    """True for literal cash / a cash sweep — value it at book, NEVER as a
    tradeable security. Detected three ways so a messy import can't slip cash
    through as a stock: a stable-value symbol, asset_class == cash, or a name
    that starts with "cash" (e.g. "Cash & Cash Investments", "Cash - Savings")."""
    sym = (getattr(asset, "symbol", None) or "").upper().strip()
    if sym in STABLE_VALUE_SYMBOLS:
        return True
    ac = getattr(asset, "asset_class", None)
    ac = ac.value if hasattr(ac, "value") else str(ac or "")
    if ac == "cash":
        return True
    return (getattr(asset, "name", None) or "").strip().lower().startswith("cash")


class MarketDataService:
    """Fetches and caches market prices for publicly traded assets."""

    def __init__(self, session: Session, cache_ttl_minutes: int = 15):
        self.session = session
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)

    def get_current_price(self, asset: Asset) -> Decimal | None:
        """Get the most recent price for an asset, fetching from yfinance if stale."""
        # Literal cash / sweeps are book value — never a tradeable security.
        # Short-circuit before any cached price or yfinance call so cash can't be
        # over-valued (a stray fetch once priced "CASH" as the PGIM ETF at ~$83)
        # OR under-valued to $0 (a missing price → a bogus −100% loss). Returns the
        # stored unit value if present (e.g. a lump-sum savings balance), else $1.
        if is_cash_asset(asset):
            # Honor only a deliberately ENTERED unit value (source="manual", e.g. a
            # savings balance set in the edit form). A stray yfinance/snapshot row
            # must never win here — that's exactly how "CASH" once priced as the
            # PGIM ETF (~$83). No manual price → book value of $1/unit.
            return self._get_latest_manual_price(asset.id, source="manual") or Decimal("1")

        if not asset.is_publicly_traded or not asset.symbol:
            # Manually-priced holding (e.g. a 401k collective investment trust with
            # no public ticker). If it has a price-proxy (look_through_ticker) and a
            # manual base price, track the proxy's daily return from that base — so
            # the holding moves day-to-day instead of sitting frozen — while staying
            # anchored to the real unit value. Read-only (DB-first; no network).
            base = self._get_latest_manual_price_row(asset.id)
            if base is not None and asset.look_through_ticker:
                ratio = self._proxy_ratio(asset.look_through_ticker, base[1])
                if ratio is not None:
                    return Decimal(str(round(float(base[0]) * ratio, 6)))
            return base[0] if base is not None else None

        cached = self._get_cached_price(asset.id)
        if cached is not None:
            return cached

        # 3-day cache miss: prefer the most recent stored price (any age) over a
        # blocking yfinance call during a page render. This keeps pages responsive
        # — and the server reachable — when offline or when prices are stale,
        # instead of hanging per-holding (which is what dumps the PWA to its
        # "You're offline" screen). Freshness comes from the explicit Refresh /
        # snapshot capture / backfill paths; only hit the network when there is no
        # stored price at all (a brand-new asset).
        latest = self._get_latest_manual_price(asset.id)
        if latest is not None:
            return latest

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

    def _held_public_assets(self) -> list[Asset]:
        """Currently-held assets that are publicly traded with a non-stable ticker."""
        held_ids = {
            aid for (aid,) in self.session.query(TaxLot.asset_id)
            .filter(TaxLot.is_closed == False).distinct()
        }
        if not held_ids:
            return []
        assets = (
            self.session.query(Asset)
            .filter(Asset.id.in_(held_ids), Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
            .all()
        )
        return [a for a in assets if (a.symbol or "").upper() not in STABLE_VALUE_SYMBOLS]

    def history_is_thin(self, min_dates: int = 200) -> bool:
        """True when held public assets lack deep price history (so a backfill is
        worthwhile). False when there are no such holdings — keeps it a no-op on
        an empty database."""
        assets = self._held_public_assets()
        if not assets:
            return False
        distinct_dates = (
            self.session.query(AssetPrice.price_date)
            .filter(AssetPrice.asset_id.in_([a.id for a in assets]))
            .distinct().count()
        )
        return distinct_dates < min_dates

    def backfill_history(self, months: int = 24) -> dict:
        """Download daily closes for held public assets and persist any *missing*
        dates into AssetPrice (idempotent). Powers real trailing-window returns and
        a robust prior-day baseline for the day-change."""
        assets = self._held_public_assets()
        # Include price-proxy tickers so proxied holdings (401k CITs) get the
        # history their ratio needs.
        for psym in self._proxy_symbols_for_held():
            ref = self._proxy_reference(psym, create=True)
            if ref is not None and ref not in assets:
                assets.append(ref)
        if not assets:
            return {"symbols": 0, "rows_added": 0}

        sym_to_id = {a.symbol: a.id for a in assets}
        symbols = list(sym_to_id.keys())
        start = date.today() - timedelta(days=int(months * 31))

        try:
            import yfinance as yf
            raw = yf.download(
                symbols, start=str(start), end=str(date.today() + timedelta(days=1)),
                auto_adjust=True, progress=False, threads=True,
            )
        except Exception:
            return {"symbols": len(symbols), "rows_added": 0}
        if raw is None or raw.empty:
            return {"symbols": len(symbols), "rows_added": 0}

        close = raw["Close"] if "Close" in getattr(raw, "columns", []) else raw
        series_by_symbol: dict[str, object] = {}
        if hasattr(close, "columns"):                       # multi-ticker DataFrame
            for sym in symbols:
                if sym in close.columns:
                    series_by_symbol[sym] = close[sym].dropna()
        elif len(symbols) == 1:                             # single-ticker Series
            series_by_symbol[symbols[0]] = close.dropna()

        rows_added = 0
        for sym, series in series_by_symbol.items():
            asset_id = sym_to_id[sym]
            existing = {
                d for (d,) in self.session.query(AssetPrice.price_date)
                .filter(AssetPrice.asset_id == asset_id).all()
            }
            for ts, price in series.items():
                d = ts.date() if hasattr(ts, "date") else ts
                if d in existing or price is None:
                    continue
                self.session.add(AssetPrice(
                    asset_id=asset_id, price_date=d,
                    close_price=Decimal(str(round(float(price), 6))), source="yfinance-backfill",
                ))
                existing.add(d)
                rows_added += 1
        self.session.flush()
        return {"symbols": len(symbols), "rows_added": rows_added}

    def repair_stable_value_prices(self) -> int:
        """Reset any stored price for stable-value holdings back to $1.00.

        get_current_price already forces $1 for these, but the morning brief,
        charts, day-change and history read ``asset_prices`` directly — so this
        scrubs stale rows (e.g. a "CASH" row left at the ~$83 PGIM ETF price)
        without depending on the one-time repair migration. Returns rows fixed.
        """
        stable_ids = [
            a.id for a in self.session.query(Asset).filter(Asset.symbol.isnot(None)).all()
            if (a.symbol or "").upper() in STABLE_VALUE_SYMBOLS
        ]
        if not stable_ids:
            return 0
        return (
            self.session.query(AssetPrice)
            .filter(AssetPrice.asset_id.in_(stable_ids), AssetPrice.close_price != 1)
            .update({AssetPrice.close_price: 1}, synchronize_session=False)
        )

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

    def _get_latest_manual_price_row(self, asset_id: int, source: str | None = None):
        """Most recent stored price as (close_price, price_date), or None.
        Pass source="manual" to consider only deliberately entered prices."""
        q = self.session.query(AssetPrice).filter(AssetPrice.asset_id == asset_id)
        if source is not None:
            q = q.filter(AssetPrice.source == source)
        p = q.order_by(AssetPrice.price_date.desc()).first()
        return (p.close_price, p.price_date) if p else None

    def _get_latest_manual_price(self, asset_id: int, source: str | None = None) -> Decimal | None:
        """Get the most recent manually entered price for private assets."""
        row = self._get_latest_manual_price_row(asset_id, source=source)
        return row[0] if row else None

    # ── Price proxies (look_through_ticker) for untickered holdings (401k CITs) ──

    def _proxy_reference(self, symbol: str, create: bool = False) -> "Asset | None":
        """Asset that carries a price-proxy ticker's history. Prefers a real
        holding with that symbol if one exists; otherwise an auto-created
        reference asset (is_reference=True, no holdings)."""
        sym = (symbol or "").upper().strip()
        if not sym:
            return None
        existing = (
            self.session.query(Asset)
            .filter(func.upper(Asset.symbol) == sym)
            .order_by(Asset.is_reference)   # real asset (False) sorts before reference (True)
            .first()
        )
        if existing or not create:
            return existing
        ref = Asset(symbol=sym, name=f"(price proxy) {sym}",
                    asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True, is_reference=True)
        self.session.add(ref)
        self.session.flush()
        return ref

    def _proxy_ratio(self, proxy_symbol: str, base_date) -> float | None:
        """proxy_latest_price / proxy_price_on_or_before(base_date), from stored
        prices only (read-only). None if the proxy has no usable history yet."""
        ref = self._proxy_reference(proxy_symbol, create=False)
        if ref is None:
            return None
        latest = (
            self.session.query(AssetPrice).filter(AssetPrice.asset_id == ref.id)
            .order_by(AssetPrice.price_date.desc()).first()
        )
        base = (
            self.session.query(AssetPrice)
            .filter(AssetPrice.asset_id == ref.id, AssetPrice.price_date <= base_date)
            .order_by(AssetPrice.price_date.desc()).first()
        )
        if latest and base and float(base.close_price) > 0:
            return float(latest.close_price) / float(base.close_price)
        return None

    def _proxy_symbols_for_held(self) -> set[str]:
        """look_through_tickers of held, manually-priced (untickered) holdings."""
        held_ids = {
            aid for (aid,) in self.session.query(TaxLot.asset_id)
            .filter(TaxLot.is_closed == False).distinct()
        }
        if not held_ids:
            return set()
        out: set[str] = set()
        for a in self.session.query(Asset).filter(Asset.id.in_(held_ids)).all():
            if (not a.is_publicly_traded or not a.symbol) and a.look_through_ticker:
                sym = a.look_through_ticker.upper().strip()
                if sym and sym not in STABLE_VALUE_SYMBOLS:
                    out.add(sym)
        return out

    def refresh_proxy_prices(self) -> int:
        """Fetch today's price for each price-proxy ticker (creating the reference
        asset if needed) so proxied holdings move on each refresh. Returns count."""
        n = 0
        for sym in self._proxy_symbols_for_held():
            ref = self._proxy_reference(sym, create=True)
            if ref and self._fetch_and_cache(ref) is not None:
                n += 1
        return n

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

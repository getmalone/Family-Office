"""Intraday portfolio price snapshots and change-since-last-reading.

Captures a price reading per (asset, day, session bucket) — pre-market, morning,
midday, evening — plus any manual refresh, so the dashboard can show the
portfolio's move since the *immediately preceding* reading: morning vs the
previous evening, midday vs morning, evening vs midday, and a manual refresh vs
whatever came right before it.

Repeated captures within the same bucket update that bucket's reading in place
(latest price wins). Stable-value holdings (CASH, money markets) are pinned to
$1.00 and never priced from yfinance — fixing the CASH-as-$83 corruption.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetPrice, PriceSnapshot
from app.models.tax_lot import TaxLot
from app.services.market_data import STABLE_VALUE_SYMBOLS
from app.services.portfolio_service import PortfolioService
from app.services.symbols import yahoo_symbol

ET = ZoneInfo("America/New_York")

# Session buckets by ET wall-clock (start time inclusive), in order.
_BUCKETS: list[tuple[str, time, str]] = [
    ("premarket", time(0, 0),  "Pre-market"),
    ("morning",   time(9, 30), "Morning"),
    ("midday",    time(12, 0), "Midday"),
    ("evening",   time(16, 0), "Evening"),
]
_BUCKET_LABEL = {key: label for key, _, label in _BUCKETS}


def _bucket_for(now_et: datetime) -> str:
    t = now_et.time()
    current = _BUCKETS[0][0]
    for key, start, _ in _BUCKETS:
        if t >= start:
            current = key
    return current


class SnapshotService:
    """Capture and compare intraday portfolio price readings."""

    STALE_MINUTES = 30  # opportunistic capture only refetches if the latest reading is older

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _now_et() -> datetime:
        return datetime.now(ET).replace(tzinfo=None)

    # ── Capture ───────────────────────────────────────────────────────────────

    def capture(self, *, force: bool = False) -> dict:
        """Fetch live prices for public holdings and record a reading.

        ``force=True`` always fetches (the manual Refresh). ``force=False`` is the
        opportunistic on-open path: it skips the network fetch if the most recent
        reading is younger than ``STALE_MINUTES``.
        """
        now = self._now_et()
        latest = (
            self.session.query(PriceSnapshot)
            .order_by(PriceSnapshot.captured_at.desc())
            .first()
        )
        if not force and latest and (now - latest.captured_at) < timedelta(minutes=self.STALE_MINUTES):
            return self.change_since_previous()

        bucket = _bucket_for(now)
        today = now.date()

        from app.services.holdings import held_asset_ids

        held_ids = held_asset_ids(self.session)
        assets = (
            self.session.query(Asset)
            .filter(Asset.id.in_(held_ids), Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
            .all()
            if held_ids else []
        )

        yf = None
        for asset in assets:
            sym = (asset.symbol or "").upper()
            if sym in STABLE_VALUE_SYMBOLS:
                price = Decimal("1")                       # never price cash from yfinance
            else:
                # Skip anything no feed can resolve (a CUSIP, a broker subtotal
                # row like CRM-TOTAL, or a malformed ticker) and use the form
                # Yahoo expects (BRK/B → BRK-B). Otherwise every capture burns a
                # round-trip per junk symbol and logs a failure.
                ysym = yahoo_symbol(asset.symbol)
                if ysym is None:
                    continue
                if yf is None:
                    import yfinance as yf  # noqa: PLC0415 — heavy, import lazily
                try:
                    hist = yf.Ticker(ysym).history(period="2d")
                    if hist.empty:
                        continue
                    price = Decimal(str(round(hist.iloc[-1]["Close"], 6)))
                except Exception:
                    continue
            self._upsert_snapshot(asset.id, today, bucket, price, now)
            self._upsert_daily_close(asset.id, today, price)

        self.session.flush()
        return self.change_since_previous()

    def _find_snapshot(self, asset_id: int, d: date, bucket: str) -> PriceSnapshot | None:
        return (
            self.session.query(PriceSnapshot)
            .filter_by(asset_id=asset_id, price_date=d, bucket=bucket)
            .first()
        )

    def _upsert_snapshot(self, asset_id: int, d: date, bucket: str, price: Decimal, when: datetime) -> None:
        row = self._find_snapshot(asset_id, d, bucket)
        if row:
            row.price, row.captured_at = price, when
            return
        # Check-then-insert races another capture (or another tab) for the same
        # (asset, day, bucket). Insert inside a SAVEPOINT so losing that race
        # rolls back just this row instead of poisoning the whole session — an
        # un-rolled-back IntegrityError here used to 500 the page at commit.
        try:
            with self.session.begin_nested():
                self.session.add(PriceSnapshot(
                    asset_id=asset_id, price_date=d, bucket=bucket, price=price, captured_at=when,
                ))
        except IntegrityError:
            existing = self._find_snapshot(asset_id, d, bucket)
            if existing:                       # the other writer won — take the newer price
                existing.price, existing.captured_at = price, when

    def _find_daily_close(self, asset_id: int, d: date) -> AssetPrice | None:
        return self.session.query(AssetPrice).filter_by(asset_id=asset_id, price_date=d).first()

    def _upsert_daily_close(self, asset_id: int, d: date, price: Decimal) -> None:
        row = self._find_daily_close(asset_id, d)
        if row:
            row.close_price, row.source = price, "snapshot"
            return
        try:
            with self.session.begin_nested():   # same race as above (uq_asset_price_date)
                self.session.add(AssetPrice(asset_id=asset_id, price_date=d, close_price=price, source="snapshot"))
        except IntegrityError:
            existing = self._find_daily_close(asset_id, d)
            if existing:
                existing.close_price, existing.source = price, "snapshot"

    # ── Change vs the previous reading ────────────────────────────────────────

    def _qty_by_asset(self) -> dict[int, Decimal]:
        from app.services.holdings import open_lots_query

        q: dict[int, Decimal] = {}
        for lot in open_lots_query(self.session).all():
            q[lot.asset_id] = q.get(lot.asset_id, Decimal("0")) + lot.remaining_quantity
        return q

    def _batches(self, limit: int = 2) -> list[tuple[date, str, datetime]]:
        """The most recent reading batches as (date, bucket, captured_at), newest first."""
        rows = self.session.query(PriceSnapshot).order_by(PriceSnapshot.captured_at.desc()).all()
        seen: set[tuple[date, str]] = set()
        out: list[tuple[date, str, datetime]] = []
        for r in rows:
            key = (r.price_date, r.bucket)
            if key not in seen:
                seen.add(key)
                out.append((r.price_date, r.bucket, r.captured_at))
            if len(out) >= limit:
                break
        return out

    def _batch_prices(self, d: date, bucket: str) -> dict[int, Decimal]:
        return {
            r.asset_id: r.price for r in
            self.session.query(PriceSnapshot).filter_by(price_date=d, bucket=bucket).all()
        }

    def change_since_previous(self) -> dict:
        """Portfolio move from the previous reading to the latest one."""
        batches = self._batches(2)
        if not batches:
            return {"available": False}

        qty = self._qty_by_asset()
        total_aum = float(PortfolioService(self.session).get_summary().total_market_value)
        cur_d, cur_b, cur_at = batches[0]

        result: dict = {
            "available": True,
            "current_label": self._label(cur_d, cur_b),
            "captured_at": cur_at.strftime("%-I:%M %p"),
            "total_aum": round(total_aum, 2),
            "has_prev": False,
            "change": 0.0,
            "change_pct": 0.0,
            "since_label": None,
        }
        if len(batches) < 2:
            return result

        cur_prices = self._batch_prices(cur_d, cur_b)
        prev_d, prev_b, _ = batches[1]
        prev_prices = self._batch_prices(prev_d, prev_b)
        common = set(cur_prices) & set(prev_prices)                       # only assets in both readings
        change = sum(float(qty.get(a, 0)) * (float(cur_prices[a]) - float(prev_prices[a])) for a in common)
        prev_total = total_aum - change
        result.update({
            "has_prev": True,
            "change": round(change, 2),
            "change_pct": round(change / prev_total * 100, 2) if prev_total else 0.0,
            "since_label": self._label(prev_d, prev_b),
        })
        return result

    def recent_readings(self) -> list[dict]:
        """Today's readings (one per bucket) with the portfolio's public market value, oldest first."""
        today = self._now_et().date()
        qty = self._qty_by_asset()
        rows = (
            self.session.query(PriceSnapshot)
            .filter(PriceSnapshot.price_date == today)
            .order_by(PriceSnapshot.captured_at)
            .all()
        )
        by_bucket: dict[str, dict] = {}
        for r in rows:
            b = by_bucket.setdefault(r.bucket, {"value": 0.0, "at": r.captured_at})
            b["value"] += float(qty.get(r.asset_id, 0)) * float(r.price)
            b["at"] = max(b["at"], r.captured_at)
        order = {k: i for i, (k, _, _) in enumerate(_BUCKETS)}
        return [
            {"bucket": k, "label": _BUCKET_LABEL.get(k, k.title()),
             "value": round(v["value"], 2), "at": v["at"].strftime("%-I:%M %p")}
            for k, v in sorted(by_bucket.items(), key=lambda kv: order.get(kv[0], 99))
        ]

    def _label(self, d: date, bucket: str) -> str:
        today = self._now_et().date()
        base = _BUCKET_LABEL.get(bucket, bucket.title())
        if d == today:
            return base
        if d == today - timedelta(days=1):
            return f"{base} (prev day)"
        return f"{base} ({d:%a %b %-d})"

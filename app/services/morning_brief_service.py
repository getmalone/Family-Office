"""
Daily Brief Service for the Family Office.

Aggregates macro market data, sector performance, and portfolio-specific
day-change impact into a single daily dashboard view.

Cache strategy: two sessions per trading day
  AM session  — midnight → 4:05 PM ET  (morning brief, pre/intraday context)
  PM session  — 4:05 PM ET → midnight  (evening report, official closing prices)

Data is re-fetched once per session on first page hit; subsequent hits within
the same session are served from memory (< 1 ms).
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetPrice
from app.models.account import Account
from app.models.tax_lot import TaxLot
from app.services.markov_regime_service import MarkovRegimeService


# ── Timezone & session constants ──────────────────────────────────────────────

ET = ZoneInfo("America/New_York")
_CLOSE_CUTOFF = time(16, 5)   # 4:05 PM ET — market close + 5 min buffer


# ── Module-level cache ────────────────────────────────────────────────────────

_brief_cache: dict[str, dict] = {}   # session_key → brief dict


def invalidate_brief_cache() -> None:
    """Clear the brief cache so the next page load rebuilds with fresh prices."""
    _brief_cache.clear()


# ── Market universe ───────────────────────────────────────────────────────────

INDICES = {
    "SPY":    "S&P 500",
    "QQQ":    "Nasdaq 100",
    "DIA":    "Dow Jones",
    "IWM":    "Russell 2000",
    "^VIX":   "VIX",
    "TLT":    "20yr Treasury",
    "GLD":    "Gold",
    "USO":    "Oil (ETF)",
    "UUP":    "US Dollar",
    "BTC-USD":"Bitcoin",
}

SECTORS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLI":  "Industrials",
    "XLC":  "Communication",
    "XLY":  "Consumer Disc",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
}

SECTOR_ETF_MAP = {v: k for k, v in SECTORS.items()}

TICKER_SECTOR_HINTS: dict[str, str] = {
    "XLK": "Technology",   "QQQ": "Technology",  "FTEC": "Technology",
    "QTUM": "Technology",  "MSTR": "Technology",
    "XLF": "Financials",   "VFH": "Financials",   "PFXF": "Financials",
    "XLE": "Energy",       "XOM": "Energy",        "CVX": "Energy",
    "COP": "Energy",       "OXY": "Energy",        "PSX": "Energy",
    "XLV": "Health Care",  "JNJ": "Health Care",   "UNH": "Health Care",
    "PFE": "Health Care",  "MRK": "Health Care",
    "XLI": "Industrials",  "CAT": "Industrials",
    "HON": "Industrials",  "GE": "Industrials",
    "GLD": "Commodities",  "GDX": "Commodities",   "OUNZ": "Commodities",
    "SLV": "Commodities",  "NRGU": "Energy",
    "BTC-USD": "Crypto",
}


class MorningBriefService:
    """
    Fetches macro context + computes portfolio day-change impact.

    Caches one brief per market session (AM / PM) so yfinance is only
    called once per session boundary, not on every page load.
    """

    def __init__(self, session: Session):
        self.session = session

    # ── Session helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _now_et() -> datetime:
        return datetime.now(ET)

    @staticmethod
    def _session_key(now_et: datetime) -> str:
        """Returns e.g. '2026-04-30-PM'.  Changes at 4:05 PM ET each weekday."""
        d = now_et.date()
        slot = "PM" if now_et.time() >= _CLOSE_CUTOFF else "AM"
        return f"{d}-{slot}"

    @staticmethod
    def _session_label(now_et: datetime) -> str:
        """Human-readable report title."""
        if now_et.weekday() >= 5:          # weekend
            return "Weekend Summary"
        if now_et.time() >= _CLOSE_CUTOFF:
            return "Evening Report"
        return "Morning Brief"

    @staticmethod
    def _next_update_str(now_et: datetime) -> str:
        """Plain-English description of when data next refreshes."""
        if now_et.weekday() >= 5:
            # next Monday pre-market
            days_ahead = 7 - now_et.weekday()
            nxt = (now_et.date() + timedelta(days=days_ahead)).strftime("%A")
            return f"Next update: {nxt} at market open"
        if now_et.time() < _CLOSE_CUTOFF:
            return "Next update: today at market close (~4:05 PM ET)"
        return "Next update: tomorrow at market open"

    # ── Public entry point ────────────────────────────────────────────────────

    def get_brief(self, force_refresh: bool = False) -> dict[str, Any]:
        now_et  = self._now_et()
        key     = self._session_key(now_et)

        if not force_refresh and key in _brief_cache:
            return _brief_cache[key]

        brief = self._build_brief(now_et)
        _brief_cache.clear()          # evict any stale sessions
        _brief_cache[key] = brief
        return brief

    # ── Build brief ───────────────────────────────────────────────────────────

    def _build_brief(self, now_et: datetime) -> dict[str, Any]:
        today     = date.today()
        yesterday = self._prev_trading_day(today)

        market_symbols = list(INDICES.keys()) + list(SECTORS.keys())
        market_data    = self._fetch_market_snapshots(market_symbols)

        indices_data = self._build_index_rows(market_data)
        sectors_data = self._build_sector_rows(market_data)

        holdings_impact, portfolio_day = self._portfolio_day_impact(today, yesterday)

        # ── Markov regime detection (portfolio-blended) ───────────────────────
        # One yfinance download (2y, period="2y") for all holding symbols.
        # The *portfolio* regime is based on the market-value-weighted composite
        # return series — not just SPY — so it reflects your actual allocation mix.
        # Per-ticker regimes come from the same download at no extra cost.
        market_regime = None
        try:
            # Build symbol → weight map from holdings market values
            total_mv = sum(h.get("market_value", 0.0) for h in holdings_impact)
            weights: dict[str, float] = {}
            for h in holdings_impact:
                sym = h.get("symbol", "—")
                mv  = h.get("market_value", 0.0)
                if sym and sym != "—" and mv > 0 and total_mv > 0:
                    weights[sym] = weights.get(sym, 0.0) + mv / total_mv

            unique_syms = list(weights.keys())

            if unique_syms:
                regime_svc = MarkovRegimeService()
                portfolio_regime, ticker_regimes = regime_svc.get_portfolio_and_ticker_regimes(
                    symbols=unique_syms,
                    weights=weights,
                )
                market_regime = portfolio_regime

                # Annotate each holding with its own signal score and regime label
                for h in holdings_impact:
                    sym = h.get("symbol", "—")
                    r = ticker_regimes.get(sym)
                    h["signal_score"] = round(r.signal_score, 3) if r else None
                    h["regime"]       = r.current_regime          if r else None
        except Exception:
            # Regime is decorative — never let it crash the brief
            for h in holdings_impact:
                h.setdefault("signal_score", None)
                h.setdefault("regime",       None)

        narrative = self._build_narrative(indices_data, sectors_data, portfolio_day, market_regime)

        slot = "PM" if now_et.time() >= _CLOSE_CUTOFF else "AM"

        return {
            "as_of":        today.strftime("%A, %B %d %Y"),
            "session":      slot,                            # "AM" | "PM"
            "report_title": self._session_label(now_et),
            "next_update":  self._next_update_str(now_et),
            "fetched_at":   now_et.strftime("%-I:%M %p ET"),
            "indices":         indices_data,
            "sectors":         sectors_data,
            "holdings_impact": holdings_impact,
            "portfolio_day":   portfolio_day,
            "narrative":       narrative,
            "market_regime":   market_regime,   # RegimeState | None
        }

    # ── Market data ───────────────────────────────────────────────────────────

    def _fetch_market_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Single yfinance bulk download for all market symbols."""
        results: dict[str, dict] = {}
        try:
            import yfinance as yf
            raw = yf.download(
                symbols,
                period="5d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                return results

            close = raw["Close"] if "Close" in raw.columns else None
            if close is None:
                return results

            for sym in symbols:
                try:
                    col = close[sym].dropna() if sym in close.columns else None
                    if col is None or len(col) < 2:
                        continue
                    prev_price = float(col.iloc[-2])
                    curr_price = float(col.iloc[-1])
                    chg_pct = (curr_price - prev_price) / prev_price * 100 if prev_price else 0
                    chg_abs = curr_price - prev_price
                    results[sym] = {
                        "price":      curr_price,
                        "prev_price": prev_price,
                        "chg_abs":    chg_abs,
                        "chg_pct":    chg_pct,
                    }
                except Exception:
                    continue
        except Exception:
            pass
        return results

    def _build_index_rows(self, market_data: dict) -> list[dict]:
        rows = []
        for sym, label in INDICES.items():
            d = market_data.get(sym)
            if not d:
                continue
            rows.append({
                "symbol":  sym,
                "label":   label,
                "price":   round(d["price"], 2),
                "chg_abs": round(d["chg_abs"], 2),
                "chg_pct": round(d["chg_pct"], 2),
                "up":      d["chg_pct"] >= 0,
            })
        return rows

    def _build_sector_rows(self, market_data: dict) -> list[dict]:
        rows = []
        for sym, label in SECTORS.items():
            d = market_data.get(sym)
            if not d:
                continue
            rows.append({
                "symbol":  sym,
                "label":   label,
                "chg_pct": round(d["chg_pct"], 2),
                "up":      d["chg_pct"] >= 0,
            })
        return sorted(rows, key=lambda r: r["chg_pct"], reverse=True)

    # ── Portfolio impact ──────────────────────────────────────────────────────

    def _portfolio_day_impact(
        self, today: date, yesterday: date
    ) -> tuple[list[dict], dict]:
        lots = self.session.query(TaxLot).filter(TaxLot.is_closed == False).all()
        if not lots:
            return [], {"total_mv": 0, "day_chg": 0, "day_chg_pct": 0, "up": True}

        asset_map: dict[int, Asset] = {}
        acct_map: dict[int, Account] = {}
        for lot in lots:
            if lot.asset_id not in asset_map:
                a = self.session.get(Asset, lot.asset_id)
                if a:
                    asset_map[lot.asset_id] = a
            if lot.account_id not in acct_map:
                ac = self.session.get(Account, lot.account_id)
                if ac:
                    acct_map[lot.account_id] = ac

        all_asset_ids = list(asset_map.keys())
        today_prices = self._price_dict(all_asset_ids, today)
        yest_prices  = self._price_dict(all_asset_ids, yesterday)

        positions: dict[tuple[int, int], dict] = {}
        for lot in lots:
            key = (lot.asset_id, lot.account_id)
            positions.setdefault(key, {"qty": Decimal("0")})
            positions[key]["qty"] += lot.remaining_quantity

        rows: list[dict] = []
        total_mv   = Decimal("0")
        total_prev = Decimal("0")

        for (asset_id, acct_id), pos in positions.items():
            asset = asset_map.get(asset_id)
            acct  = acct_map.get(acct_id)
            if not asset or not acct:
                continue

            qty    = pos["qty"]
            curr_p = today_prices.get(asset_id) or yest_prices.get(asset_id) or Decimal("0")
            prev_p = yest_prices.get(asset_id) or curr_p

            mv      = qty * curr_p
            prev_mv = qty * prev_p
            day_chg = mv - prev_mv
            day_pct = float(day_chg / prev_mv * 100) if prev_mv else 0.0

            total_mv   += mv
            total_prev += prev_mv

            sector = TICKER_SECTOR_HINTS.get(asset.symbol or "", "")

            rows.append({
                "symbol":       asset.symbol or "—",
                "name":         asset.name[:40],
                "account":      acct.name,
                "qty":          float(qty),
                "curr_price":   float(curr_p),
                "prev_price":   float(prev_p),
                "market_value": float(mv),
                "day_chg":      float(day_chg),
                "day_pct":      round(day_pct, 2),
                "up":           day_chg >= 0,
                "sector":       sector,
                "has_move":     abs(day_pct) >= 0.5,
            })

        rows.sort(key=lambda r: abs(r["day_chg"]), reverse=True)

        port_day_chg = float(total_mv - total_prev)
        port_day_pct = (port_day_chg / float(total_prev) * 100) if total_prev else 0.0

        return rows, {
            "total_mv":    float(total_mv),
            "day_chg":     port_day_chg,
            "day_chg_pct": round(port_day_pct, 2),
            "up":          port_day_chg >= 0,
        }

    def _price_dict(self, asset_ids: list[int], target_date: date) -> dict[int, Decimal]:
        cutoff = target_date - timedelta(days=5)
        rows = (
            self.session.query(AssetPrice)
            .filter(
                AssetPrice.asset_id.in_(asset_ids),
                AssetPrice.price_date >= cutoff,
                AssetPrice.price_date <= target_date,
            )
            .order_by(AssetPrice.price_date.desc())
            .all()
        )
        result: dict[int, Decimal] = {}
        for row in rows:
            if row.asset_id not in result:
                result[row.asset_id] = row.close_price
        return result

    # ── Narrative ─────────────────────────────────────────────────────────────

    def _build_narrative(
        self,
        indices: list[dict],
        sectors: list[dict],
        portfolio_day: dict,
        market_regime=None,
    ) -> list[str]:
        points: list[str] = []

        spy = next((i for i in indices if i["symbol"] == "SPY"), None)
        vix = next((i for i in indices if i["symbol"] == "^VIX"), None)
        tlt = next((i for i in indices if i["symbol"] == "TLT"), None)
        gld = next((i for i in indices if i["symbol"] == "GLD"), None)
        btc = next((i for i in indices if i["symbol"] == "BTC-USD"), None)

        if spy:
            tone = "rallying" if spy["chg_pct"] > 0.5 else \
                   "selling off" if spy["chg_pct"] < -0.5 else "flat"
            dir_str = f"+{spy['chg_pct']:.2f}%" if spy["up"] else f"{spy['chg_pct']:.2f}%"
            points.append(
                f"Broad market is {tone} — S&P 500 {dir_str} at ${spy['price']:,.2f}."
            )

        if vix:
            if vix["price"] > 30:
                points.append(f"⚠️  VIX at {vix['price']:.1f} — high fear. Expect elevated volatility.")
            elif vix["price"] > 20:
                points.append(f"VIX at {vix['price']:.1f} — moderate uncertainty in the market.")
            else:
                points.append(f"VIX at {vix['price']:.1f} — calm conditions, low implied volatility.")

        if tlt and spy:
            if tlt["up"] and spy["up"]:
                points.append("Bonds and stocks both up — risk-on but also rate expectations easing.")
            elif not tlt["up"] and not spy["up"]:
                points.append("Both stocks and bonds down — broad risk-off, possibly macro driven.")
            elif not tlt["up"] and spy["up"]:
                points.append("Rates rising (TLT down) as stocks climb — growth optimism outweighing rate pressure.")

        if sectors:
            best  = sectors[0]
            worst = sectors[-1]
            points.append(
                f"Sector leaders: {best['label']} ({'+' if best['up'] else ''}{best['chg_pct']:.2f}%). "
                f"Laggards: {worst['label']} ({'+' if worst['up'] else ''}{worst['chg_pct']:.2f}%)."
            )

        if gld:
            if gld["chg_pct"] > 1.0:
                points.append(f"Gold surging +{gld['chg_pct']:.2f}% — safe-haven demand elevated. Positive for GDX, OUNZ, SLV positions.")
            elif gld["chg_pct"] < -1.0:
                points.append(f"Gold down {gld['chg_pct']:.2f}% — risk appetite improving, pressure on precious metals holdings.")

        if btc and abs(btc["chg_pct"]) > 2:
            dir_str = f"+{btc['chg_pct']:.1f}%" if btc["up"] else f"{btc['chg_pct']:.1f}%"
            points.append(f"Bitcoin {dir_str} — crypto allocation impacted.")

        # Markov regime bullet
        if market_regime is not None:
            r = market_regime
            stat = r.stationary
            sig_str = f"+{r.signal_score:.2f}" if r.signal_score >= 0 else f"{r.signal_score:.2f}"
            if r.current_regime == "Bull":
                points.append(
                    f"🐂 Markov regime: Bull (signal {sig_str}). "
                    f"Long-run equilibrium: {stat.bull * 100:.0f}% Bull / "
                    f"{stat.sideways * 100:.0f}% Sideways / {stat.bear * 100:.0f}% Bear."
                )
            elif r.current_regime == "Bear":
                points.append(
                    f"🐻 Markov regime: Bear (signal {sig_str}). "
                    f"⚠️  Elevated drawdown risk. "
                    f"Long-run equilibrium: {stat.bull * 100:.0f}% Bull / "
                    f"{stat.bear * 100:.0f}% Bear."
                )
            else:
                points.append(
                    f"↔️  Markov regime: Sideways (signal {sig_str}). "
                    f"No clear directional momentum from the last {r.lookback_days // 21:.0f} months of SPY data."
                )

        chg  = portfolio_day["day_chg"]
        pct  = portfolio_day["day_chg_pct"]
        sign = "+" if chg >= 0 else ""
        points.append(
            f"Your portfolio is {sign}${chg:,.0f} ({sign}{pct:.2f}%) today "
            f"(total AUM ${portfolio_day['total_mv']:,.0f})."
        )

        return points

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _prev_trading_day(d: date) -> date:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

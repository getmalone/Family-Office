"""Detect and repair holdings that can't be priced.

Some imports land a security's CUSIP in the ``symbol`` field (e.g. a 401(k)
fund shown as ``31617E745``), others carry broker notation or dead tickers.
yfinance can't resolve those, so the holding gets no market price — it shows
$0 P&L and is held flat in returns.

``resolve_and_fix`` runs an escalating pipeline over each such holding:

  1. If the symbol *looks* valid, price-check it directly — a brand-new holding
     may simply be waiting on backfill, and second-guessing a working ticker is
     how mis-mappings happen.
  2. CUSIP → ticker via the free OpenFIGI mapping API (deterministic; the
     CUSIP moves into ``Asset.cusip``).
  3. Yahoo's own search, queried with the asset's *name* from the import.
     Candidates are filtered to US venues and sane quote types, scored by name
     similarity, and price-validated before anything is accepted:
       • score ≥ 0.85 → auto-applied, with provenance in resolution_source
       • score ≥ 0.40 → stored on the asset as a one-click suggestion
  4. Nothing anywhere → ``resolution_status="no_listing"``: almost certainly a
     collective investment trust or other unlisted fund. Terminal — later runs
     skip it and the UI routes the user to proxy pricing (look_through_ticker)
     instead of retrying a lookup that can never succeed.

A wrong mapping silently corrupts valuations, so the scorer is deliberately
conservative: corporate suffix noise ("Inc", "Corp") is ignored, but share-
class and instrument words ("Class B", "ETF", "Fund") are NOT — an 0.82 match
like "Berkshire Hathaway Class B" → "Berkshire Hathaway Inc. New" becomes a
suggestion for the user to confirm, never an auto-apply.
"""
from __future__ import annotations

import json
import re
import urllib.request
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetPrice
from app.models.tax_lot import TaxLot
from app.services.market_data import STABLE_VALUE_SYMBOLS
from app.services.render_guard import network_allowed
from app.services.symbols import (  # noqa: F401 — looks_like_cusip re-exported
    looks_like_cusip,
    looks_like_rollup,
    yahoo_symbol,
)

_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Score thresholds for name-search candidates. Auto-apply is intentionally
# strict — below it, the user confirms with one click instead.
_AUTO_APPLY_SCORE = 0.85
_SUGGEST_SCORE = 0.40

_QUOTE_TYPES_OK = {"EQUITY", "ETF", "MUTUALFUND", "MONEYMARKET", "INDEX", "CRYPTOCURRENCY"}
# Yahoo exchange codes for US venues (+ crypto). A foreign listing of the same
# name is the wrong instrument here — different currency, different close.
_US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "PCX", "ASE", "BTS", "CCC"}

# Corporate-suffix noise that varies between a brokerage export and Yahoo's
# listing name without changing identity. Share-class / instrument words
# (Class, ETF, Fund, Trust) are deliberately NOT here — they distinguish
# instruments that price differently.
_NAME_NOISE = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "PLC", "LTD", "LIMITED", "THE", "NEW",
}


def _norm_tokens(name: str | None) -> list[str]:
    s = re.sub(r"[^A-Z0-9& ]+", " ", (name or "").upper())
    return [t for t in s.split() if t not in _NAME_NOISE]


def name_match_score(a: str | None, b: str | None) -> float:
    """0..1 similarity between two security names; order-insensitive at best."""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    s1 = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    s2 = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(s1, s2)


class SymbolService:
    def __init__(self, session: Session):
        self.session = session
        self._price_ok_cache: dict[str, bool] = {}

    def _held_public_assets(self) -> list[Asset]:
        from app.services.holdings import held_asset_ids

        held_ids = held_asset_ids(self.session)
        if not held_ids:
            return []
        assets = (
            self.session.query(Asset)
            .filter(Asset.id.in_(held_ids), Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
            .all()
        )
        return [a for a in assets if (a.symbol or "").upper() not in STABLE_VALUE_SYMBOLS]

    def unpriceable_assets(self) -> list[Asset]:
        """Held public holdings that can't be priced: a symbol no feed can resolve
        (a CUSIP, a broker subtotal row like ``CRM-TOTAL``, or otherwise malformed),
        or no price history at all."""
        out = []
        for a in self._held_public_assets():
            if yahoo_symbol(a.symbol) is None:
                out.append(a)
                continue
            has_price = (
                self.session.query(AssetPrice.id)
                .filter(AssetPrice.asset_id == a.id).first()
            )
            if not has_price:
                out.append(a)
        return out

    # ── Resolution pipeline ──────────────────────────────────────────────────

    def resolve_and_fix(self, force: bool = False) -> dict:
        """Run the full resolution pipeline over every unpriceable holding.

        Returns a report:
          resolved     — mappings applied (deterministic CUSIP hit or ≥0.85 name
                         match, both price-validated)
          suggested    — candidate stored on the asset for one-click confirm
          no_listing   — every avenue exhausted; flagged terminal (skipped on
                         later runs unless ``force``) so the UI can route to
                         proxy pricing instead
          verified_ok  — symbol actually prices fine; just awaiting backfill
          unresolved   — couldn't resolve, but not provably unlisted (kept
                         eligible for future runs)
        """
        report: dict[str, list] = {
            "resolved": [], "suggested": [], "no_listing": [],
            "verified_ok": [], "unresolved": [],
        }
        todo = [
            a for a in self.unpriceable_assets()
            if force or a.resolution_status != "no_listing"
        ]
        if not todo:
            return report

        cusip_assets = [a for a in todo if looks_like_cusip(a.symbol)]
        figi_map = self._openfigi_map([a.symbol.upper() for a in cusip_assets])

        for a in todo:
            sym = (a.symbol or "").upper().strip()

            # 1) A valid-looking symbol that simply has no stored rows yet may
            #    price perfectly well — leave it for the backfill, don't remap.
            ysym = yahoo_symbol(sym)
            if ysym is not None and self._prices_ok(ysym):
                report["verified_ok"].append({"symbol": sym, "name": a.name})
                continue

            # 2) Deterministic CUSIP → ticker (OpenFIGI), price-validated.
            figi_ticker = figi_map.get(sym) if looks_like_cusip(sym) else None
            if figi_ticker:
                target = yahoo_symbol(figi_ticker) or figi_ticker
                if self._prices_ok(target):
                    self._apply(a, target, source=f"openfigi:{sym}")
                    report["resolved"].append(
                        {"from": sym, "ticker": target, "name": a.name, "via": "openfigi"})
                    continue

            # 3) Yahoo search by the asset's imported name.
            best = self._best_name_match(a.name)
            if best is not None:
                score, cand_sym, cand_name, qt, exch = best
                if score >= _AUTO_APPLY_SCORE and self._prices_ok(cand_sym):
                    self._apply(a, cand_sym, source="yahoo-search:name")
                    report["resolved"].append(
                        {"from": sym, "ticker": cand_sym, "name": a.name,
                         "via": f"yahoo-search {int(score * 100)}% name match"})
                    continue
                if score >= _SUGGEST_SCORE and self._prices_ok(cand_sym):
                    a.suggested_symbol = cand_sym
                    a.suggested_note = (
                        f"{cand_name} ({qt.title()}, {exch}) — {int(score * 100)}% name match")
                    a.resolution_status = "suggested"
                    report["suggested"].append(
                        {"symbol": sym, "name": a.name,
                         "suggested": cand_sym, "note": a.suggested_note})
                    continue

            # 4) Dead end. A CUSIP/rollup/malformed symbol that no directory or
            #    search knows is, in practice, an unlisted instrument — mark it
            #    terminal so we stop burning lookups on it. A well-formed ticker
            #    stays eligible (could be a fat-finger the user will correct).
            if ysym is None:
                a.resolution_status = "no_listing"
                report["no_listing"].append({"symbol": sym, "name": a.name})
            else:
                report["unresolved"].append({"symbol": sym, "name": a.name})

        self.session.flush()
        return report

    def apply_suggestion(self, asset_id: int) -> dict | None:
        """Apply the stored suggestion for one asset (the user clicked accept)."""
        a = self.session.get(Asset, asset_id)
        if a is None or not a.suggested_symbol:
            return None
        old = (a.symbol or "").upper().strip()
        self._apply(a, a.suggested_symbol, source=f"user-confirmed:yahoo-search (was {old})")
        self.session.flush()
        return {"asset_id": a.id, "from": old, "ticker": a.symbol, "name": a.name}

    def _apply(self, a: Asset, ticker: str, source: str) -> None:
        sym = (a.symbol or "").upper().strip()
        if looks_like_cusip(sym) and not a.cusip:
            a.cusip = sym
        a.symbol = ticker
        a.resolution_source = source
        a.resolution_status = None
        a.suggested_symbol = None
        a.suggested_note = None

    # ── Lookup backends (isolated for tests; all no-ops outside allow_network) ─

    def _prices_ok(self, symbol: str) -> bool:
        """Does Yahoo actually return recent prices for this symbol? Cached per
        run so the same candidate isn't fetched twice."""
        cached = self._price_ok_cache.get(symbol)
        if cached is not None:
            return cached
        ok = False
        if network_allowed():
            try:
                import yfinance as yf
                h = yf.Ticker(symbol).history(period="5d")
                ok = h is not None and not h.empty
            except Exception:
                ok = False
        self._price_ok_cache[symbol] = ok
        return ok

    def _best_name_match(self, name: str | None):
        """Best US-listed candidate for a security name, or None.

        Returns (score, symbol, candidate_name, quote_type, exchange)."""
        if not (name or "").strip():
            return None
        try:
            quotes = self._yahoo_search(name.strip())
        except Exception:
            return None
        best = None
        for q in quotes:
            sym = (q.get("symbol") or "").upper().strip()
            qt = (q.get("quoteType") or "").upper()
            exch = (q.get("exchange") or "").upper()
            cand_name = q.get("shortname") or q.get("longname") or ""
            if not sym or qt not in _QUOTE_TYPES_OK or exch not in _US_EXCHANGES:
                continue
            if yahoo_symbol(sym) != sym:      # keep to plain US-form symbols
                continue
            score = name_match_score(name, cand_name)
            if best is None or score > best[0]:
                best = (score, sym, cand_name, qt, exch)
        return best

    @staticmethod
    def _yahoo_search(query: str) -> list[dict]:
        """Raw Yahoo search hits for a free-text query (name, usually)."""
        if not network_allowed():
            return []
        import yfinance as yf
        s = yf.Search(query, max_results=8)
        return list(s.quotes or [])

    @staticmethod
    def _openfigi_map(cusips: list[str]) -> dict[str, str]:
        """Map CUSIP → ticker via OpenFIGI. Best-effort; returns {} on any error."""
        if not cusips or not network_allowed():
            return {}
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
        req = urllib.request.Request(
            _OPENFIGI_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.load(resp)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for cusip, item in zip(cusips, data):
            if isinstance(item, dict) and item.get("data"):
                ticker = item["data"][0].get("ticker")
                if ticker:
                    out[cusip] = ticker.upper()
        return out

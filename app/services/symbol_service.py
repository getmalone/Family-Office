"""Detect and repair holdings that can't be priced.

Some imports land a security's CUSIP in the ``symbol`` field (e.g. a 401(k)
fund shown as ``31617E745``). yfinance can't resolve a CUSIP, so the holding
gets no market price — it shows $0 P&L and is held flat in returns. This service
flags those holdings and best-effort maps the CUSIP to a real ticker via the
free OpenFIGI mapping API, moving the CUSIP into the dedicated ``cusip`` field.
"""
from __future__ import annotations

import json
import urllib.request

from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetPrice
from app.models.tax_lot import TaxLot
from app.services.market_data import STABLE_VALUE_SYMBOLS
from app.services.symbols import (  # noqa: F401 — looks_like_cusip re-exported
    looks_like_cusip,
    looks_like_rollup,
    yahoo_symbol,
)

_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


class SymbolService:
    def __init__(self, session: Session):
        self.session = session

    def _held_public_assets(self) -> list[Asset]:
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

    @staticmethod
    def _openfigi_map(cusips: list[str]) -> dict[str, str]:
        """Map CUSIP → ticker via OpenFIGI. Best-effort; returns {} on any error."""
        if not cusips:
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

    def resolve_and_fix(self) -> dict:
        """Resolve CUSIP-symbol holdings to tickers and update them in place.

        Moves the CUSIP into ``Asset.cusip`` and sets ``Asset.symbol`` to the
        resolved ticker. Holdings that are unpriceable for other reasons (unknown
        ticker) are reported for manual correction.
        """
        unpriceable = self.unpriceable_assets()
        cusip_assets = [a for a in unpriceable if looks_like_cusip(a.symbol)]
        mapping = self._openfigi_map([a.symbol.upper() for a in cusip_assets])

        resolved, unresolved = [], []
        for a in unpriceable:
            sym = (a.symbol or "").upper()
            ticker = mapping.get(sym) if looks_like_cusip(sym) else None
            if ticker:
                if not a.cusip:
                    a.cusip = sym
                a.symbol = ticker
                resolved.append({"cusip": sym, "ticker": ticker, "name": a.name})
            else:
                unresolved.append({"symbol": a.symbol, "name": a.name})
        self.session.flush()
        return {"resolved": resolved, "unresolved": unresolved}

"""
Price update routes for the Family Office dashboard.

Provides manual price refresh and shows last-updated status.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.asset import Asset, AssetPrice
from app.services.render_guard import allow_network

router = APIRouter()


@router.post("/refresh")
def refresh_prices(db: Session = Depends(get_db)):
    """Capture a fresh intraday price reading and return the change vs the
    previous reading. Stable-value holdings (CASH, money markets) are pinned to
    $1.00 and never priced from yfinance."""
    from app.services.snapshot_service import SnapshotService
    from app.services.market_data import MarketDataService

    # This is THE explicit refresh path — the one place we deliberately go to the
    # network. allow_network() lifts the render guard so price fetching works
    # (ordinary page renders stay DB-only and never block).
    with allow_network():
        # One-time: pull and store deep history so trailing-window returns are real
        # and the day-change has a solid prior-day baseline. Idempotent + gated, so
        # it only does heavy work on the first refresh after a fresh import.
        try:
            md = MarketDataService(db)
            md.repair_stable_value_prices()   # scrub any stale CASH/money-market rows back to $1
            if md.history_is_thin():
                md.backfill_history()
            md.refresh_proxy_prices()         # update price-proxy tickers (401k CIT tracking)
        except Exception:
            pass

        try:
            result = SnapshotService(db).capture(force=True)
        except ImportError:
            return JSONResponse({"error": "yfinance not installed"}, status_code=500)

    # Bust the morning brief cache so the next page load reflects the new prices.
    try:
        from app.services.morning_brief_service import invalidate_brief_cache
        invalidate_brief_cache()
    except Exception:
        pass

    return JSONResponse(result)


@router.post("/resolve-symbols")
def resolve_symbols(force: bool = False, db: Session = Depends(get_db)):
    """Run the ticker-resolution pipeline over unpriceable holdings: verify the
    current symbol, map CUSIPs via OpenFIGI, then search Yahoo by security name
    (auto-applying only high-confidence, price-validated matches; storing the
    rest as one-click suggestions; flagging provable dead ends "no_listing").
    Backfills history for anything that resolved. ``force=true`` re-checks
    holdings previously flagged no_listing."""
    from app.services.symbol_service import SymbolService
    from app.services.market_data import MarketDataService

    with allow_network():
        report = SymbolService(db).resolve_and_fix(force=force)
        if report.get("resolved"):
            try:
                MarketDataService(db).backfill_history()
            except Exception:
                pass
    return JSONResponse(report)


@router.post("/apply-suggestion")
def apply_suggestion(asset_id: int, db: Session = Depends(get_db)):
    """Apply a stored ticker suggestion (user clicked accept on the dashboard
    banner), then backfill the renamed holding's history so it prices now."""
    from app.services.symbol_service import SymbolService
    from app.services.market_data import MarketDataService

    with allow_network():
        result = SymbolService(db).apply_suggestion(asset_id)
        if result is None:
            return JSONResponse({"error": "no suggestion stored for this asset"}, status_code=404)
        try:
            MarketDataService(db).backfill_history()
        except Exception:
            pass
    return JSONResponse(result)


@router.get("/status")
def price_status(db: Session = Depends(get_db)):
    """Check when prices were last updated."""
    today = date.today()
    total_public = (
        db.query(Asset)
        .filter(Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
        .count()
    )
    updated_today = (
        db.query(AssetPrice)
        .filter(AssetPrice.price_date == today)
        .count()
    )
    latest = (
        db.query(AssetPrice)
        .order_by(AssetPrice.price_date.desc())
        .first()
    )

    return JSONResponse({
        "total_public_assets": total_public,
        "updated_today": updated_today,
        "latest_price_date": str(latest.price_date) if latest else None,
    })

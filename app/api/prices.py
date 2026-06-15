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

router = APIRouter()


@router.post("/refresh")
def refresh_prices(db: Session = Depends(get_db)):
    """Capture a fresh intraday price reading and return the change vs the
    previous reading. Stable-value holdings (CASH, money markets) are pinned to
    $1.00 and never priced from yfinance."""
    from app.services.snapshot_service import SnapshotService

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

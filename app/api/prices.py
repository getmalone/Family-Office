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
    """Fetch live prices for all publicly traded assets. Returns JSON summary."""
    assets = (
        db.query(Asset)
        .filter(Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
        .all()
    )

    today = date.today()
    results = {}

    try:
        import yfinance as yf
    except ImportError:
        return JSONResponse({"error": "yfinance not installed"}, status_code=500)

    for asset in assets:
        try:
            ticker = yf.Ticker(asset.symbol)
            hist = ticker.history(period="2d")
            if hist.empty:
                results[asset.symbol] = "no data"
                continue

            row = hist.iloc[-1]
            price = Decimal(str(round(row["Close"], 6)))

            existing = (
                db.query(AssetPrice)
                .filter(AssetPrice.asset_id == asset.id, AssetPrice.price_date == today)
                .first()
            )
            if existing:
                existing.close_price = price
                existing.source = "yfinance"
            else:
                db.add(AssetPrice(
                    asset_id=asset.id, price_date=today,
                    close_price=price, source="yfinance",
                ))
            results[asset.symbol] = str(price)

        except Exception as e:
            results[asset.symbol] = f"error: {e}"

    db.flush()

    # Bust the morning brief cache so the next page load reflects the new prices
    try:
        from app.services.morning_brief_service import invalidate_brief_cache
        invalidate_brief_cache()
    except Exception:
        pass

    return JSONResponse({
        "date": str(today),
        "updated": len([v for v in results.values() if v not in ("no data",) and not v.startswith("error")]),
        "total": len(assets),
        "prices": results,
    })


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

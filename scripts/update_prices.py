#!/usr/bin/env python3
"""
Daily price updater for the Family Office.

Fetches current prices for all publicly traded assets via yfinance
and updates the asset_prices cache. Run daily via cron or scheduler.

Usage:
    python scripts/update_prices.py
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.asset import Asset, AssetPrice
from app.services.db import init_db, get_db_session


def update_prices():
    """Fetch and cache current prices for all publicly traded assets."""
    engine, factory = init_db()
    today = date.today()

    with get_db_session(factory) as session:
        assets = (
            session.query(Asset)
            .filter(Asset.is_publicly_traded == True, Asset.symbol.isnot(None))
            .all()
        )

        if not assets:
            print("No publicly traded assets found.")
            return

        print(f"Updating prices for {len(assets)} assets...")

        import yfinance as yf

        success = 0
        for asset in assets:
            try:
                ticker = yf.Ticker(asset.symbol)
                hist = ticker.history(period="2d")
                if hist.empty:
                    print(f"  {asset.symbol}: no data")
                    continue

                row = hist.iloc[-1]
                price = Decimal(str(round(row["Close"], 6)))
                high = Decimal(str(round(row.get("High", 0), 6)))
                low = Decimal(str(round(row.get("Low", 0), 6)))
                volume = int(row.get("Volume", 0))

                existing = (
                    session.query(AssetPrice)
                    .filter(AssetPrice.asset_id == asset.id, AssetPrice.price_date == today)
                    .first()
                )
                if existing:
                    existing.close_price = price
                    existing.high_price = high
                    existing.low_price = low
                    existing.volume = volume
                    existing.source = "yfinance"
                else:
                    session.add(AssetPrice(
                        asset_id=asset.id, price_date=today,
                        close_price=price, high_price=high,
                        low_price=low, volume=volume,
                        source="yfinance",
                    ))

                success += 1
                print(f"  {asset.symbol}: ${price}")

            except Exception as e:
                print(f"  {asset.symbol}: error - {e}")

        session.flush()
        print(f"\nDone. Updated {success}/{len(assets)} prices for {today}.")


if __name__ == "__main__":
    update_prices()

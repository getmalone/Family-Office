"""
Dashboard route — main landing page for the Family Office.

Shows KPI cards (total AUM, unrealized P&L, realized YTD, pending approvals),
asset allocation, and recent activity.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.approval import Approval, ApprovalStatusEnum
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Render the main Family Office dashboard."""
    market = MarketDataService(db)
    portfolio_svc = PortfolioService(db, market)
    tax_svc = TaxService(db, market)

    try:
        summary = portfolio_svc.get_summary()
    except Exception:
        from app.schemas.portfolio import PortfolioSummary
        from decimal import Decimal
        summary = PortfolioSummary(
            total_market_value=Decimal("0"),
            total_cost_basis=Decimal("0"),
            total_unrealized_gain_loss=Decimal("0"),
            total_realized_ytd=Decimal("0"),
            day_change=Decimal("0"),
            holdings=[],
            allocation={},
        )

    try:
        tax_summary = tax_svc.get_tax_summary()
    except Exception:
        tax_summary = None

    pending_count = (
        db.query(Approval)
        .filter(Approval.status == ApprovalStatusEnum.PENDING)
        .count()
    )

    # Trailing-window performance (3M / 6M / 12M / YTD). Cached ~60 min; best-effort
    # so a slow/failed price fetch never blocks the landing page.
    try:
        from app.services.analysis_service import AnalysisService
        period_returns = AnalysisService(db).get_period_returns()
    except Exception:
        period_returns = []

    # If the windows are empty because the price history is still thin, kick a
    # background backfill (guarded + cooldown'd, so cheap to call every load) and
    # tell the panel to show "building…" instead of "not enough history".
    history_building = False
    try:
        if any(not r.get("available") for r in period_returns):
            from app.services import backfill_worker
            backfill_worker.ensure_history()
            history_building = backfill_worker.is_running()
    except Exception:
        history_building = False

    # Holdings that can't be priced (CUSIP-as-symbol or unknown ticker) — surfaced
    # so the user can map them to real tickers. Cheap DB-only check (no network).
    # Carries any stored resolver state so the banner can offer a one-click
    # suggestion or point no-listing holdings at the price-proxy flow.
    try:
        from app.services.symbol_service import SymbolService
        unpriceable = [
            {
                "id": a.id,
                "symbol": a.symbol,
                "name": a.name,
                "status": a.resolution_status,
                "suggested_symbol": a.suggested_symbol,
                "suggested_note": a.suggested_note,
            }
            for a in SymbolService(db).unpriceable_assets()
        ]
    except Exception:
        unpriceable = []

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "summary": summary,
            "tax_summary": tax_summary,
            "pending_approvals": pending_count,
            "period_returns": period_returns,
            "history_building": history_building,
            "unpriceable": unpriceable,
            "page_title": "Dashboard",
        },
    )

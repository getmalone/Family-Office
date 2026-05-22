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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "summary": summary,
            "tax_summary": tax_summary,
            "pending_approvals": pending_count,
            "page_title": "Dashboard",
        },
    )

"""
Tax optimization routes for the Family Office dashboard.

Provides tax-loss harvest candidates, election simulation, and tax summary views.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.market_data import MarketDataService
from app.services.tax_service import TaxService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/harvest")
def harvest_candidates(request: Request, db: Session = Depends(get_db)):
    """Show tax-loss harvest candidates."""
    svc = TaxService(db)
    candidates = svc.find_harvest_candidates()

    return templates.TemplateResponse(
        "tax/harvest.html",
        {
            "request": request,
            "candidates": candidates,
            "page_title": "Tax-Loss Harvesting",
        },
    )


@router.get("/summary")
def tax_summary(request: Request, tax_year: int | None = None, db: Session = Depends(get_db)):
    """Show annual tax summary."""
    svc = TaxService(db)
    summary = svc.get_tax_summary(tax_year)

    return templates.TemplateResponse(
        "tax/summary.html",
        {
            "request": request,
            "summary": summary,
            "page_title": "Tax Summary",
        },
    )


@router.get("/elections")
def election_simulation(request: Request, db: Session = Depends(get_db)):
    """Show tax election simulation tool."""
    svc = TaxService(db)
    basis_results = svc.simulate_election("cost_basis_method")
    sec475_results = svc.simulate_election("section_475")

    return templates.TemplateResponse(
        "tax/election_sim.html",
        {
            "request": request,
            "basis_results": basis_results,
            "sec475_results": sec475_results,
            "page_title": "Election Simulation",
        },
    )

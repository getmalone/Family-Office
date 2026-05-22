"""
Estate planning routes for the Family Office dashboard.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.family import FamilyMember
from app.schemas.estate import GiftCreate, GRATParams
from app.services.estate_service import EstateService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/ownership")
def ownership_tree(request: Request, db: Session = Depends(get_db)):
    """Show family ownership tree."""
    svc = EstateService(db)
    tree = svc.get_ownership_tree()

    return templates.TemplateResponse(
        "estate/ownership.html",
        {"request": request, "tree": tree, "page_title": "Ownership"},
    )


@router.get("/gifts")
def gift_log(
    request: Request,
    tax_year: int | None = None,
    db: Session = Depends(get_db),
):
    """Show gift log with annual/lifetime exclusion tracking."""
    svc = EstateService(db)
    gifts = svc.get_gifts(tax_year=tax_year or date.today().year)
    members = db.query(FamilyMember).filter(FamilyMember.is_active == True).all()

    return templates.TemplateResponse(
        "estate/gifting.html",
        {
            "request": request,
            "gifts": gifts,
            "members": members,
            "tax_year": tax_year or date.today().year,
            "page_title": "Gifts",
        },
    )


@router.post("/gifts")
def record_gift(
    request: Request,
    donor_id: int = Form(...),
    recipient_id: int = Form(...),
    gift_date: str = Form(...),
    fair_market_value: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Record a new gift."""
    svc = EstateService(db)
    svc.record_gift(
        GiftCreate(
            donor_member_id=donor_id,
            recipient_member_id=recipient_id,
            gift_date=date.fromisoformat(gift_date),
            fair_market_value=Decimal(str(fair_market_value)),
            notes=notes,
        )
    )
    return RedirectResponse(url="/estate/gifts", status_code=303)


@router.get("/grat")
def grat_simulation(request: Request, db: Session = Depends(get_db)):
    """Show GRAT simulation tool."""
    members = db.query(FamilyMember).filter(FamilyMember.is_active == True).all()

    return templates.TemplateResponse(
        "estate/grat_sim.html",
        {"request": request, "members": members, "results": None, "page_title": "GRAT Simulation"},
    )


@router.post("/grat/simulate")
def run_grat_simulation(
    request: Request,
    name: str = Form(...),
    grantor_id: int = Form(...),
    initial_funding: float = Form(...),
    term_years: int = Form(2),
    section_7520_rate: float = Form(5.0),
    db: Session = Depends(get_db),
):
    """Run a GRAT simulation."""
    svc = EstateService(db)
    members = db.query(FamilyMember).filter(FamilyMember.is_active == True).all()

    params = GRATParams(
        name=name,
        grantor_member_id=grantor_id,
        initial_funding=Decimal(str(initial_funding)),
        term_years=term_years,
        section_7520_rate=Decimal(str(section_7520_rate)),
    )
    results = svc.simulate_grat(params)

    return templates.TemplateResponse(
        "estate/grat_sim.html",
        {
            "request": request,
            "members": members,
            "results": results,
            "params": params,
            "page_title": "GRAT Simulation",
        },
    )

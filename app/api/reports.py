"""
Reporting routes for the Family Office dashboard.
"""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.reporting_service import ReportingService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
def reports_list(request: Request, db: Session = Depends(get_db)):
    """List generated reports."""
    svc = ReportingService(db)
    reports = svc.get_reports()
    flags = svc.get_compliance_flags()

    return templates.TemplateResponse(
        "reports/list.html",
        {
            "request": request,
            "reports": reports,
            "flags": flags,
            "page_title": "Reports",
        },
    )


@router.post("/generate")
def generate_report(
    request: Request,
    report_type: str = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    db: Session = Depends(get_db),
):
    """Generate a new report."""
    svc = ReportingService(db)

    if report_type == "quarterly_summary":
        svc.generate_quarterly_report(
            date.fromisoformat(period_start),
            date.fromisoformat(period_end),
        )
    elif report_type == "schedule_d":
        year = date.fromisoformat(period_start).year
        svc.generate_schedule_d_data(year)

    return RedirectResponse(url="/reports/", status_code=303)

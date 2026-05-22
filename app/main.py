"""
FastAPI application factory for the Family Office dashboard.

This is a dedicated private investment and wealth management platform
providing portfolio monitoring, tax optimization, accounting,
compliance reporting, estate planning, and human-in-the-loop approval workflows.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.db import init_db

app = FastAPI(
    title="Family Office",
    description="Agentic AI Wealth Management System",
    version="0.1.0",
)

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Template engine
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)

# Import and register routers
from app.api.dashboard import router as dashboard_router
from app.api.portfolio import router as portfolio_router
from app.api.tax import router as tax_router
from app.api.accounting import router as accounting_router
from app.api.reports import router as reports_router
from app.api.estate import router as estate_router
from app.api.approvals import router as approvals_router
from app.api.agent_chat import router as agent_router
from app.api.health import router as health_router
from app.api.analysis import router as analysis_router
from app.api.prices import router as prices_router

app.include_router(dashboard_router)
app.include_router(portfolio_router, prefix="/portfolio", tags=["Portfolio"])
app.include_router(tax_router, prefix="/tax", tags=["Tax"])
app.include_router(accounting_router, prefix="/accounting", tags=["Accounting"])
app.include_router(reports_router, prefix="/reports", tags=["Reports"])
app.include_router(estate_router, prefix="/estate", tags=["Estate"])
app.include_router(analysis_router, prefix="/analysis", tags=["Analysis"])
app.include_router(approvals_router, prefix="/approvals", tags=["Approvals"])
app.include_router(agent_router, prefix="/agent", tags=["Agent"])
app.include_router(prices_router, prefix="/prices", tags=["Prices"])
app.include_router(health_router, tags=["Health"])


# Wiki route
from fastapi.responses import HTMLResponse
from pathlib import Path as _Path

@app.get("/wiki/FamilyOffice/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki/FamilyOffice", response_class=HTMLResponse, include_in_schema=False)
async def wiki():
    wiki_path = _Path(__file__).parent / "templates" / "wiki" / "index.html"
    return HTMLResponse(content=wiki_path.read_text(), status_code=200)


@app.on_event("startup")
def startup():
    """Initialize database on server startup."""
    init_db()

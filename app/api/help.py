"""
In-app Help page — renders the end-user guide (USAGE.md) as HTML.

Single source of truth: the same USAGE.md that ships in the distribution bundle
is rendered here so the guide is reachable while using the app.
"""

from pathlib import Path

import markdown as md
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_USAGE = Path(__file__).parent.parent.parent / "USAGE.md"


@router.get("/help", response_class=HTMLResponse, include_in_schema=False)
def help_page(request: Request):
    try:
        text = _USAGE.read_text(encoding="utf-8")
        body = md.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    except FileNotFoundError:
        body = "<p>User guide not found.</p>"
    return templates.TemplateResponse(
        "help.html",
        {"request": request, "doc": body, "page_title": "Help"},
    )

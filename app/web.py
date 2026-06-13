"""
Web access control for network/internet exposure.

The Family Office was built local-first (127.0.0.1, single user). To make it
safely usable "via web" — reachable from a phone, another machine on the LAN,
or a deployment — this module adds an *optional* shared-passcode gate.

Behaviour:
  - If ``settings.access_code`` is empty (the default), the gate is a no-op and
    the app behaves exactly as before. This keeps the trusted local workflow.
  - If ``settings.access_code`` is set, every request must carry a valid signed
    session cookie, obtained by entering the code on ``/login``. A small set of
    paths stay open so the PWA shell can install and boot offline before login.

The session cookie is stateless: its value is an HMAC of a constant message
keyed by the access code, so rotating the code invalidates all sessions and no
server-side session store is needed.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import settings

COOKIE_NAME = "kfo_session"
_AUTH_MESSAGE = b"kfo-auth-v1"

# Paths reachable without authentication: the PWA shell, health check, and the
# login flow itself. Everything else is gated when an access code is configured.
_OPEN_PREFIXES = ("/static/", "/login")
_OPEN_EXACT = {
    "/manifest.webmanifest",
    "/service-worker.js",
    "/offline",
    "/help",
    "/health",
    "/favicon.ico",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _expected_token(code: str) -> str:
    """Deterministic session token derived from the access code."""
    return hmac.new(code.encode("utf-8"), _AUTH_MESSAGE, hashlib.sha256).hexdigest()


def is_authenticated(request: Request) -> bool:
    """True if no code is required, or the request carries a valid session cookie."""
    code = settings.access_code
    if not code:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(token, _expected_token(code))


def _is_open_path(path: str) -> bool:
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIXES)


class AccessCodeMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated browsers to /login; 401 API/HTMX calls."""

    async def dispatch(self, request: Request, call_next):
        if not settings.access_code or _is_open_path(request.url.path):
            return await call_next(request)

        if is_authenticated(request):
            return await call_next(request)

        # HTMX / fetch / non-GET requests get a clean 401 instead of an HTML page.
        wants_html = request.method == "GET" and "text/html" in request.headers.get(
            "accept", ""
        )
        if wants_html and "hx-request" not in request.headers:
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            return RedirectResponse(url=f"/login?next={nxt}", status_code=303)
        return Response("Authentication required", status_code=401)


router = APIRouter()


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request, next: str = "/", error: str = ""):
    # Already authenticated (or no code required) → go straight in.
    if is_authenticated(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next or "/", "error": error},
    )


@router.post("/login", include_in_schema=False)
def login_submit(request: Request, code: str = Form(...), next: str = Form("/")):
    if not settings.access_code or not hmac.compare_digest(
        code, settings.access_code
    ):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next or "/", "error": "Incorrect access code."},
            status_code=401,
        )

    # Only allow same-site relative redirects to avoid open-redirect abuse.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(url=target, status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        _expected_token(settings.access_code),
        max_age=settings.session_max_age_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


@router.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

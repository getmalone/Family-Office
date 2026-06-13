"""
Progressive Web App routes.

Serves the web manifest, service worker, and offline fallback page from the
site root so the service worker's scope covers the whole origin ("/"). These
endpoints are deliberately unauthenticated (see app.web.AccessCodeMiddleware)
so the app shell can install and boot offline before any login.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(
        _STATIC / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/service-worker.js", include_in_schema=False)
def service_worker() -> Response:
    """Served from root so the SW controls the entire origin scope."""
    body = (_STATIC / "service-worker.js").read_bytes()
    return Response(
        content=body,
        media_type="application/javascript",
        headers={
            # Allow root scope and prevent the SW file itself from being cached stale.
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline() -> HTMLResponse:
    return HTMLResponse((_STATIC / "offline.html").read_text())

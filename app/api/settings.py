"""
Settings page: configure the app (API key, model), back up the database, and
import accounts/positions from CSV — all without touching files on disk.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services import app_settings
from app.services.import_service import import_positions_data
from app.version import get_app_version

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
def settings_page(request: Request, db: Session = Depends(get_db), msg: str = "", err: str = ""):
    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "values": app_settings.masked_settings(db),
            "version": get_app_version(),
            "msg": msg,
            "err": err,
            "page_title": "Settings",
        },
    )


@router.get("/check-updates")
def check_updates():
    """Return current/latest version + whether an update is available (JSON).

    no-store: the PWA service worker must never serve a stale answer here — a
    cached response once reported a long-gone version as "current".
    """
    from app.services.updater import get_update_status

    return JSONResponse(
        get_update_status(fetch=True),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/apply-update")
def apply_update_now():
    """Install the available update in place.

    Zip installs download the latest GitHub Release bundle and swap the code
    (data/.env/.venv untouched); git installs check out the tag. Either way the
    user relaunches to finish — the launcher re-syncs dependencies.
    """
    from app.services import updater

    status = updater.get_update_status(fetch=True)
    if not status["update_available"]:
        return JSONResponse({"ok": False, "message": "no update available"}, status_code=409)
    if status["is_git"]:
        ok, msg = updater.apply_update(status["latest"])
    else:
        ok, msg = updater.apply_manual_update()
    return JSONResponse(
        {"ok": ok, "message": msg, "restart_required": ok},
        status_code=200 if ok else 500,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/")
def save_settings(
    db: Session = Depends(get_db),
    anthropic_api_key: str = Form(""),
    llm_model: str = Form(""),
):
    """Persist settings to the encrypted DB and apply them immediately."""
    # Only overwrite the API key when a new value is typed (blank = keep current).
    if anthropic_api_key.strip():
        app_settings.set_setting(db, "anthropic_api_key", anthropic_api_key.strip())
        app_settings.apply_to_runtime("anthropic_api_key", anthropic_api_key.strip())
    if llm_model.strip():
        app_settings.set_setting(db, "llm_model", llm_model.strip())
        app_settings.apply_to_runtime("llm_model", llm_model.strip())
    return RedirectResponse(url="/settings/?msg=Settings+saved", status_code=303)


@router.post("/backup")
def backup(db: Session = Depends(get_db)):
    """Create an encrypted backup and download it."""
    try:
        path = app_settings.backup_database()
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/settings/?err=Backup+failed:+{exc}", status_code=303)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.post("/import")
async def import_csv(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    """Import accounts/positions from an uploaded CSV, JSON, or XML export."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        result = import_positions_data(db, raw, filename=file.filename)
        db.commit()   # persist before the backfill worker opens its own session
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(url=f"/settings/?err=Import+failed:+{exc}", status_code=303)

    # Pull ~2 years of price history for the newly imported holdings in the
    # background so the trailing-window Performance panel (3M/6M/12M/YTD) fills
    # in. Non-blocking — the import response returns immediately.
    try:
        from app.services.backfill_worker import ensure_history
        ensure_history(force=True)
    except Exception:
        pass
    summary = (
        f"Imported {result['positions']} positions ({result.get('format', '').upper()}) — "
        f"{result['accounts']} new accounts, {result['assets']} new securities."
    )
    if result.get("replaced"):
        summary += f" Replaced {result['replaced']} previously imported position(s)."
    if result.get("kept"):
        summary += f" Kept {result['kept']} position(s) with sale history."
    if result["errors"]:
        summary += f" {len(result['errors'])} row(s) skipped."
    return RedirectResponse(url=f"/settings/?msg={summary.replace(' ', '+')}", status_code=303)

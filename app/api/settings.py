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
    """Return current/latest version + whether an update is available (JSON)."""
    from app.services.updater import get_update_status

    return JSONResponse(get_update_status(fetch=True))


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
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/settings/?err=Import+failed:+{exc}", status_code=303)
    summary = (
        f"Imported {result['positions']} positions ({result.get('format', '').upper()}) — "
        f"{result['accounts']} new accounts, {result['assets']} new securities."
    )
    if result["errors"]:
        summary += f" {len(result['errors'])} row(s) skipped."
    return RedirectResponse(url=f"/settings/?msg={summary.replace(' ', '+')}", status_code=303)

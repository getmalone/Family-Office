"""
Settings page: configure the app (API key, model), back up the database, and
import accounts/positions from CSV — all without touching files on disk.
"""

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services import app_settings
from app.services import import_repair
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
            "duplicates": import_repair.scan(db),
            "version": get_app_version(),
            "msg": msg,
            "err": err,
            "page_title": "Settings",
        },
    )


@router.post("/upload-update")
async def upload_update(file: UploadFile = File(...)):
    """Install an update from a release zip the user downloaded in the browser.

    The way out when the app itself cannot reach GitHub — a proxy, a firewall,
    or TLS interception on the network. The browser can fetch the file; the app
    verifies and installs what it is handed, exactly as an automatic update
    does.
    """
    from app.services import updater

    from app.services import restart

    data = await file.read()
    ok, message = updater.apply_bundle_file(data)
    restarting = ok and restart.request_restart()
    return JSONResponse({"ok": ok, "message": message, "restarting": restarting},
                        headers={"Cache-Control": "no-store"})


@router.post("/repair-duplicates")
def repair_duplicates(db: Session = Depends(get_db)):
    """Remove positions left stacked by the pre-v0.1.21 append-only importer.

    Keeps each account's most recent upload and drops the ones it superseded, so
    an account that has drifted since its last export is repaired without having
    to re-upload a file that is now out of date.
    """
    try:
        app_settings.backup_database()  # the DB is about to lose rows
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings/?err=Backup+failed,+nothing+removed:+{exc}", status_code=303)
    try:
        report = import_repair.repair(db)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(url=f"/settings/?err=Repair+failed:+{exc}", status_code=303)

    if not report["removable"]:
        return RedirectResponse(url="/settings/?msg=No+duplicate+positions+found.",
                                status_code=303)
    summary = (
        f"Removed {report['removable']} duplicate position(s) across "
        f"{len(report['accounts'])} account(s). A backup was saved first."
    )
    if report["kept"]:
        summary += f" Kept {report['kept']} with sale history."
    return RedirectResponse(url=f"/settings/?msg={summary.replace(' ', '+')}", status_code=303)


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
    (data/.env/.venv untouched); git installs check out the tag. When the
    desktop launcher is running the app it then restarts it in place;
    otherwise the user relaunches.
    """
    from app.services import restart, updater

    status = updater.get_update_status(fetch=True)
    if not status["update_available"]:
        return JSONResponse({"ok": False, "message": "no update available"}, status_code=409)
    if status["is_git"]:
        ok, msg = updater.apply_update(status["latest"])
    else:
        ok, msg = updater.apply_manual_update()
    restarting = ok and restart.request_restart()
    return JSONResponse(
        {"ok": ok, "message": msg, "restart_required": ok and not restarting,
         "restarting": restarting},
        status_code=200 if ok else 500,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/")
def save_settings(
    db: Session = Depends(get_db),
    anthropic_api_key: str = Form(""),
    anthropic_workspace_id: str = Form(""),
    llm_model: str = Form(""),
):
    """Persist settings to the encrypted DB and apply them immediately."""
    # The workspace ID field shows its current value, so a blank submit clears it.
    app_settings.set_setting(db, "anthropic_workspace_id", anthropic_workspace_id.strip())
    app_settings.apply_to_runtime("anthropic_workspace_id", anthropic_workspace_id.strip())
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
    if result.get("reactivated"):
        summary += (f" Reactivated {result['reactivated']} account(s) that had been "
                    "deactivated.")
    if result.get("replaced"):
        summary += f" Replaced {result['replaced']} previously imported position(s)."
    if result.get("kept"):
        summary += f" Kept {result['kept']} position(s) with sale history."
    if result["errors"]:
        summary += f" {len(result['errors'])} row(s) skipped."
    return RedirectResponse(url=f"/settings/?msg={summary.replace(' ', '+')}", status_code=303)


# ── Screenshot import (Claude reads account screenshots → review → import) ────

@router.post("/import-screenshots")
def import_screenshots(
    request: Request,
    db: Session = Depends(get_db),
    files: list[UploadFile] = File(...),
):
    """Read one account's screenshots with Claude and show an editable review.
    Nothing is saved here, and the images are never written anywhere."""
    from app.models.account import Account
    from app.services import screenshot_import as si

    images = []
    for f in files:
        data = f.file.read()
        if data:
            images.append((data, (f.content_type or "").lower()))
    try:
        # Network call first, with no database transaction open.
        result = si.extract_positions(images)
    except si.ScreenshotImportError as exc:
        return RedirectResponse(url=f"/settings/?err={quote_plus(str(exc))}#screenshots",
                                status_code=303)
    finally:
        del images

    accounts = db.query(Account).filter(Account.is_active == True).order_by(Account.name).all()
    acct = result["account"]
    # Suggest an existing account whose name carries the same last 4 digits.
    match = None
    if acct["account_last4"]:
        match = next((a for a in accounts if acct["account_last4"] in (a.name or "")), None)
    return templates.TemplateResponse(
        "settings/screenshot_review.html",
        {
            "request": request,
            "page_title": "Review screenshot import",
            "account": acct,
            "rows": result["rows"],
            "notes": result["notes"],
            "accounts": accounts,
            "matched_account": match.name if match else None,
            "default_name": si.default_account_name(acct),
            "account_types": si.ACCOUNT_TYPES,
            "asset_classes": si.ASSET_CLASSES,
        },
    )


@router.post("/import-screenshots/confirm")
def confirm_screenshot_import(
    db: Session = Depends(get_db),
    target: str = Form("new"),
    existing_account: str = Form(""),
    account_name: str = Form(""),
    account_type: str = Form("brokerage"),
    institution: str = Form(""),
    include: list[str] = Form([]),
    symbol: list[str] = Form([]),
    name: list[str] = Form([]),
    asset_class: list[str] = Form([]),
    quantity: list[str] = Form([]),
    price: list[str] = Form([]),
    cost_basis_total: list[str] = Form([]),
):
    """Import the reviewed rows into one account (replacing that account's
    previously imported positions, as with file imports)."""
    from app.models.account import Account
    from app.services import screenshot_import as si
    from app.services.import_service import import_rows

    if target == "existing":
        acct = db.query(Account).filter(Account.name == existing_account).first()
        if acct is None:
            return RedirectResponse(url="/settings/?err=Choose+an+account#screenshots", status_code=303)
        acct_name = acct.name
        acct_type = getattr(acct.account_type, "value", acct.account_type)
        inst = acct.institution
    else:
        acct_name = account_name.strip() or "Imported account"
        acct_type = account_type if account_type in si.ACCOUNT_TYPES else "brokerage"
        inst = institution.strip() or None

    keep = {int(i) for i in include if i.isdigit()}
    reviewed = [
        {"symbol": symbol[i].strip().upper() or None, "name": name[i].strip(),
         "asset_class": asset_class[i], "quantity": quantity[i], "price": price[i],
         "cost_basis_total": cost_basis_total[i]}
        for i in range(len(symbol)) if i in keep
    ]
    rows = si.to_import_rows(acct_name, acct_type, inst, reviewed)
    if not rows:
        return RedirectResponse(url="/settings/?err=No+positions+selected+to+import#screenshots",
                                status_code=303)
    try:
        result = import_rows(db, rows)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(url=f"/settings/?err={quote_plus(f'Import failed: {exc}')}#screenshots",
                                status_code=303)

    try:
        from app.services.backfill_worker import ensure_history
        ensure_history(force=True)
    except Exception:
        pass
    summary = f"Imported {result['positions']} positions into {acct_name}."
    if result.get("replaced"):
        summary += f" Replaced {result['replaced']} previously imported position(s)."
    if result["errors"]:
        summary += f" {len(result['errors'])} row(s) skipped."
    return RedirectResponse(url=f"/settings/?msg={quote_plus(summary)}", status_code=303)

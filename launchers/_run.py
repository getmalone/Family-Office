"""
Cross-platform launcher for the Family Office desktop app.

Invoked by the per-OS double-click scripts after the Python environment is ready.
Responsibilities (all platform-independent, so they live here rather than being
duplicated in shell/batch):

  1. Ask for the master password (once), unless KFO_MASTER_PASSWORD is preset.
     On first run (no database yet) the password is confirmed twice — a typo here
     would otherwise lock the user out of their own data forever.
  2. Pre-flight the database: open it with the password (or create+encrypt it on
     first run). A wrong password fails here with a friendly message instead of a
     raw stack trace from the server.
  3. Pick a free port, start the web server, and open the browser once it's up.
  4. Restart in place after an in-app update: stop the server, re-sync the
     locked dependencies, and start again on the same port without asking for
     the password again (it's still in this process's environment).

Data (database, backups, .env) lives in the per-user data folder from
app/paths.py, not in this install folder; older installs are moved there once.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _find_free_port(preferred: int = 8000) -> int:
    """Return the preferred port if free, otherwise an OS-assigned free one."""
    for candidate in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", candidate))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def _prompt_master_password(db_exists: bool) -> str:
    if db_exists:
        return getpass.getpass("Enter your Family Office password: ")
    print("First run — choose a password to encrypt your data.")
    print("IMPORTANT: this password cannot be recovered. Store it somewhere safe.\n")
    pw = getpass.getpass("Create password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("\nPasswords did not match. Please run the app again.")
        raise SystemExit(1)
    return pw


def _wait_then_open(browse_url: str, host: str, port: int) -> None:
    for _ in range(120):  # up to ~60s
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    if not os.environ.get("KFO_NO_BROWSER"):
        webbrowser.open(browse_url)


def _maybe_self_update() -> None:
    """Offer (and apply) an update from the latest release tag — git installs only.

    Mode via KFO_UPDATE_MODE: 'prompt' (default), 'auto', or 'off'. Offline-safe.
    On a successful update we re-exec so the new code + dependencies take effect.
    """
    if os.environ.get("KFO_UPDATED"):  # already re-execed after applying an update
        return
    mode = os.environ.get("KFO_UPDATE_MODE", "prompt").strip().lower()
    if mode == "off":
        return
    try:
        from app.services.updater import get_update_status, apply_update

        status = get_update_status(fetch=True)
    except Exception:
        return
    if not status.get("update_available"):
        return

    current, latest = status["current"], status["latest"]
    print(f"\n⬆️  Update available: {current} → {latest}")
    if mode == "prompt":
        try:
            if input("   Update now? [Y/n] ").strip().lower() in ("n", "no"):
                print("   Skipped — you can update next time.\n")
                return
        except EOFError:
            return
    print("   Backing up your data and updating…")
    ok, msg = apply_update(latest)
    if not ok:
        print(f"   ⚠️  Update failed ({msg}). Continuing on {current}.\n")
        return
    print(f"   ✅ {msg}. Restarting…\n")
    os.environ["KFO_UPDATED"] = "1"
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


def _clear_quarantine() -> None:
    """macOS: drop the "downloaded from the internet" flag from the app folder so
    replaced files (after an update or a manual re-download) don't send you back
    to Privacy & Security to allow the launcher again."""
    if sys.platform == "darwin" and shutil.which("xattr"):
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(ROOT)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _restart(port: int) -> None:
    """Start a fresh copy of this launcher in place (new code + dependencies),
    on the same port, without re-prompting or opening another browser tab."""
    os.environ.update(KFO_PORT=str(port), KFO_NO_BROWSER="1", KFO_UPDATED="1")
    uv = shutil.which("uv")
    if uv:
        print("   Installing any new dependencies…")
        subprocess.run([uv, "sync", "--frozen"], cwd=ROOT, check=False)
        cmd = [uv, "run", "--frozen", "python", str(Path(__file__).resolve())]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve())]
    print("   Restarting…\n")
    if os.name == "nt":     # execv doesn't replace the console process on Windows
        raise SystemExit(subprocess.call(cmd, cwd=ROOT))
    os.execv(cmd[0], cmd)


# Packages the app imports lazily (so a broken install only shows up when a
# feature is used). Checked at launch; a failure triggers one clean reinstall.
_HEALTH_IMPORTS = "import anthropic, langchain_anthropic, numpy, pandas, sqlalchemy, uvicorn"


def _ensure_packages_healthy() -> None:
    """Reinstall the locked packages once if any fail to import — e.g. files
    left half-written by an interrupted install show up as a confusing
    'partially initialized module (circular import)' error much later."""
    if os.environ.get("KFO_PACKAGES_REPAIRED"):
        return
    check = subprocess.run([sys.executable, "-c", _HEALTH_IMPORTS], cwd=ROOT,
                           capture_output=True, text=True)
    if check.returncode == 0:
        return
    uv = shutil.which("uv")
    print("⚠️  Some of the app's Python packages are damaged:")
    print("   " + (check.stderr.strip().splitlines() or ["unknown error"])[-1])
    if not uv:
        print("   Reinstall them with:  uv sync --frozen --reinstall\n")
        return
    print("   Reinstalling them (one time, needs internet)…")
    subprocess.run([uv, "sync", "--frozen", "--reinstall"], cwd=ROOT, check=False)
    os.environ["KFO_PACKAGES_REPAIRED"] = "1"
    cmd = [uv, "run", "--frozen", "python", str(Path(__file__).resolve())]
    print("   Done — starting the app.\n")
    if os.name == "nt":
        raise SystemExit(subprocess.call(cmd, cwd=ROOT))
    os.execv(cmd[0], cmd)


def main() -> None:
    os.chdir(ROOT)  # so a relative KFO_DB_PATH resolves under the app folder
    _clear_quarantine()
    _ensure_packages_healthy()

    from app.paths import default_db_path, migrate_legacy_data
    try:
        for line in migrate_legacy_data(ROOT):
            print(f"📁 {line}")
    except OSError as exc:
        print(f"\n❌ Couldn't move your data to the new data folder: {exc}")
        print("   Nothing was changed. Free some disk space and try again.")
        raise SystemExit(1)

    db_path = os.environ.get("KFO_DB_PATH") or default_db_path()
    db_exists = Path(db_path).exists()

    if not os.environ.get("KFO_MASTER_PASSWORD"):
        pw = _prompt_master_password(db_exists)
        if not pw:
            print("No password entered — exiting.")
            raise SystemExit(1)
        os.environ["KFO_MASTER_PASSWORD"] = pw

    # ── Check for and (optionally) apply an update before starting ──────────────
    _maybe_self_update()

    # ── Pre-flight: open (or create + encrypt) the database with this password ──
    from app.config import Settings
    from app.services.db import create_db_engine, create_tables

    cfg = Settings()
    try:
        engine = create_db_engine(cfg)
        create_tables(engine)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT count(*) FROM sqlite_master")
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - surface a friendly message either way
        if db_exists:
            print("\n❌ Incorrect password — could not open your database.")
        else:
            print(f"\n❌ Could not create the database: {exc}")
        raise SystemExit(1)

    if not db_exists:
        print("✅ Created a new encrypted database.\n")

    # ── Start the server and open the browser ──────────────────────────────────
    host = os.environ.get("KFO_HOST", "127.0.0.1")
    port = int(os.environ.get("KFO_PORT") or _find_free_port())
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    browse_url = f"http://{browse_host}:{port}"

    print(f"\n🔓 Family Office is running at {browse_url}")
    print("   Keep this window open while you use the app. Close it to stop.\n")

    threading.Thread(
        target=_wait_then_open, args=(browse_url, browse_host, port), daemon=True
    ).start()

    import uvicorn
    from app.services import restart

    server = uvicorn.Server(uvicorn.Config("app.main:app", host=host, port=port, log_level="warning"))
    restart_requested = threading.Event()

    def _stop_for_restart() -> None:
        restart_requested.set()
        server.should_exit = True

    restart.register(_stop_for_restart)
    server.run()
    if restart_requested.is_set():
        print("\n⬆️  Update installed.")
        _restart(port)


if __name__ == "__main__":
    main()

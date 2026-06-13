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
"""

from __future__ import annotations

import getpass
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


def main() -> None:
    os.chdir(ROOT)  # so a relative KFO_DB_PATH resolves under the app folder

    db_path = os.environ.get("KFO_DB_PATH", "data/family_office.db")
    db_exists = Path(db_path).exists()

    if not os.environ.get("KFO_MASTER_PASSWORD"):
        pw = _prompt_master_password(db_exists)
        if not pw:
            print("No password entered — exiting.")
            raise SystemExit(1)
        os.environ["KFO_MASTER_PASSWORD"] = pw

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

    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

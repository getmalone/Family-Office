"""Where the app keeps its data — outside the install folder.

The encrypted database, its backups and the ``.env`` settings file live in a
per-user data folder, so updating (or reinstalling) the app's code never has to
move them:

  macOS    ~/Library/Application Support/FamilyOffice
  Windows  %APPDATA%\\FamilyOffice
  Linux    $XDG_DATA_HOME/FamilyOffice  (default ~/.local/share/FamilyOffice)

``KFO_DATA_DIR`` overrides the location; ``KFO_DB_PATH`` still overrides just the
database file. Installs from before this change kept everything in the app
folder (``data/`` and ``.env``); ``migrate_legacy_data`` moves those across once.

Standard library only: the launcher imports this before the app's settings.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "family_office.db"


def data_home() -> Path:
    override = os.environ.get("KFO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FamilyOffice"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "FamilyOffice"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "FamilyOffice"


def default_db_path() -> str:
    return str(data_home() / DB_NAME)


def env_files() -> tuple[str, ...]:
    """Settings files, lowest priority first: a ``.env`` still in the app folder
    (developer checkouts), then the one in the data folder."""
    return (str(APP_ROOT / ".env"), str(data_home() / ".env"))


def _same_size(a: Path, b: Path) -> bool:
    return b.exists() and a.stat().st_size == b.stat().st_size


def migrate_legacy_data(root: Path = APP_ROOT) -> list[str]:
    """One-time move of ``<app>/data/`` and ``<app>/.env`` into the data folder.

    Runs only when the data folder has no database yet and the app folder has
    one. Files are copied and size-checked first; only then are the originals
    renamed (``data.moved-YYYYMMDD``, ``.env.moved``) so nothing reads stale
    copies, and they stay on disk as a fallback. Returns what was done.
    """
    home = data_home()
    old_data = root / "data"
    old_env = root / ".env"
    if os.environ.get("KFO_DB_PATH") or (home / DB_NAME).exists() or not (old_data / DB_NAME).exists():
        return []

    home.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for src in old_data.rglob("*"):
        if src.is_file():
            dst = home / src.relative_to(old_data)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if not _same_size(src, dst):
                raise OSError(f"copy of {src.name} to {home} didn't verify — nothing was moved")
    done.append(f"Moved your database and backups to {home}")
    if old_env.is_file() and not (home / ".env").exists():
        shutil.copy2(old_env, home / ".env")
        if not _same_size(old_env, home / ".env"):
            raise OSError(f"copy of .env to {home} didn't verify")
        old_env.rename(root / ".env.moved")
        done.append("Moved your .env settings file there too")

    stamp = datetime.now().strftime("%Y%m%d")
    target = root / f"data.moved-{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = root / f"data.moved-{stamp}-{n}"
    old_data.rename(target)
    done.append(f"The old copy is kept at {target.name} in the app folder (safe to delete later)")
    return done

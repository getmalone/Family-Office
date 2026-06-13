"""
Read/write application settings stored in the (encrypted) database, and apply
them to the live ``settings`` object so the rest of the app needs no changes.

User-configurable keys map 1:1 onto attributes of ``app.config.Settings``:
saving the Anthropic API key here makes the agents pick it up exactly as if it
had been set via ``KFO_ANTHROPIC_API_KEY`` — no .env editing required.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings as runtime_settings
from app.models.app_setting import AppSetting

# Keys exposed on the Settings page → attribute on Settings. Values stored in DB
# take effect only when the corresponding environment variable was NOT set, so
# an explicit KFO_* env var always wins.
EDITABLE_KEYS = ("anthropic_api_key", "llm_model")

# Never echo these back to the browser in full.
SECRET_KEYS = {"anthropic_api_key", "openai_api_key"}


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(AppSetting, key)
    return row.value if row and row.value is not None else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def apply_to_runtime(key: str, value: str) -> None:
    """Update the in-memory Settings object so changes take effect immediately."""
    if hasattr(runtime_settings, key):
        setattr(runtime_settings, key, value)


def load_into_settings(session: Session) -> None:
    """At startup, fill blank Settings attributes from the database.

    Environment variables win: a value is only applied when the current setting
    is empty (i.e. not provided via KFO_* / .env).
    """
    try:
        rows = session.query(AppSetting).all()
    except Exception:
        return  # table may not exist yet on a brand-new database
    for row in rows:
        if row.key in EDITABLE_KEYS and row.value and not getattr(runtime_settings, row.key, ""):
            apply_to_runtime(row.key, row.value)


def masked_settings(session: Session) -> dict[str, str]:
    """Current effective values for display — secrets shown only as 'set/not set'."""
    out: dict[str, str] = {}
    for key in EDITABLE_KEYS:
        effective = getattr(runtime_settings, key, "") or get_setting(session, key)
        if key in SECRET_KEYS:
            out[key] = "set" if effective else ""
        else:
            out[key] = effective
    return out


def backup_database() -> Path:
    """Make a timestamped copy of the (encrypted) database file.

    The WAL is checkpointed first so the copy is self-contained. The backup is
    the SQLCipher file itself, so it stays encrypted.
    """
    from app.services.db import get_engine

    src = Path(runtime_settings.db_path)
    if not src.exists():
        raise FileNotFoundError(f"database not found at {src}")

    # Fold the write-ahead log into the main file so the copy is complete.
    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    backups = src.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = backups / f"{src.stem}-{stamp}{src.suffix}"
    shutil.copy2(src, dst)
    return dst

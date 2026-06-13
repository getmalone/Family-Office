"""
Apply database schema migrations with Alembic.

Strategy on startup:
  - Brand-new database (no app tables): ``upgrade head`` creates the full schema.
  - Existing pre-Alembic database (app tables but no ``alembic_version``):
    ``stamp head`` — adopt the current schema as the baseline, no DDL, no risk.
  - Already tracked: ``upgrade head`` applies any pending migrations, taking an
    encrypted backup first when there's something to apply.

Migrations run through the app's SQLCipher-aware engine (see alembic/env.py).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, inspect

from app.config import Settings, settings as default_settings

_ROOT = Path(__file__).resolve().parent.parent.parent
# A core table that indicates an already-initialized application database.
_SENTINEL_TABLE = "investment_profiles"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


def run_migrations(engine: Engine, cfg: Settings | None = None) -> str:
    """Bring the database schema to head. Returns the action taken."""
    cfg = cfg or default_settings
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    tables = set(inspect(engine).get_table_names())
    has_alembic = "alembic_version" in tables
    has_app = _SENTINEL_TABLE in tables
    acfg = _alembic_config()

    # Existing database created before Alembic → adopt as baseline (no schema change).
    if has_app and not has_alembic:
        command.stamp(acfg, "head")
        return "stamped"

    head = ScriptDirectory.from_config(acfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    if current == head:
        return "current"

    # Pending migrations. Back up an existing on-disk database before changing it.
    if has_app and cfg.db_path not in ("", ":memory:"):
        try:
            from app.services.app_settings import backup_database

            backup_database()
        except Exception:
            pass

    command.upgrade(acfg, "head")
    return "upgraded"

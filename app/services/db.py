"""
Database engine and session management for the Family Office.

Creates an SQLite database with WAL mode for concurrent reads and
foreign key enforcement. The database file is stored locally and
can optionally be encrypted via sqlcipher.
"""

from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, settings
from app.models.base import Base


def is_db_encrypted(cfg: Settings | None = None) -> bool:
    """True when a passphrase is configured, i.e. the DB is encrypted at rest."""
    cfg = cfg or settings
    return bool(cfg.db_passphrase)


def _sqlcipher_key_pragma(passphrase: str) -> str:
    """Build the ``PRAGMA key`` statement, escaping the passphrase safely.

    SQLCipher runs its own KDF over the passphrase, so a plain quoted string is
    the correct form. Single quotes are doubled to avoid SQL breakage.
    """
    escaped = passphrase.replace("'", "''")
    return f"PRAGMA key = '{escaped}'"


def create_db_engine(cfg: Settings | None = None) -> Engine:
    """Create and configure the SQLAlchemy engine.

    When ``cfg.db_passphrase`` is set, the database is opened through SQLCipher
    (transparent AES-256 encryption at rest); otherwise a plain SQLite file is
    used. Encryption is opt-in via ``KFO_DB_PASSPHRASE`` and fully backward
    compatible with existing unencrypted databases.
    """
    cfg = cfg or settings

    # Ensure data directory exists (skip for in-memory databases).
    if cfg.db_path != ":memory:":
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    passphrase = cfg.db_passphrase or ""
    encrypted = bool(passphrase)

    engine_kwargs: dict = dict(echo=False, pool_pre_ping=True)
    if encrypted:
        try:
            from sqlcipher3 import dbapi2 as sqlcipher
        except ImportError as exc:  # pragma: no cover - only without the wheel
            raise RuntimeError(
                "KFO_DB_PASSPHRASE is set (encrypted database requested) but the "
                "'sqlcipher3-wheels' package is not installed. "
                "Run: uv pip install sqlcipher3-wheels"
            ) from exc
        engine_kwargs["module"] = sqlcipher

    engine = create_engine(cfg.db_url, **engine_kwargs)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # PRAGMA key MUST be the first statement on a SQLCipher connection,
        # before any other access to the database.
        if encrypted:
            cursor.execute(_sqlcipher_key_pragma(passphrase))
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Wait (rather than immediately erroring) for a contended write lock —
        # important when several devices load pages that refresh live prices at once.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def create_tables(engine: Engine) -> None:
    """Create all tables from the ORM models, then apply incremental migrations."""
    Base.metadata.create_all(engine)
    _apply_migrations(engine)


def _apply_migrations(engine: Engine) -> None:
    """Idempotent column migrations for SQLite.

    ALTER TABLE ADD COLUMN raises OperationalError if the column already exists,
    so each statement is wrapped in its own try/except.  Safe to run on every
    startup — existing columns are silently skipped.
    """
    from sqlalchemy import text

    migrations = [
        "ALTER TABLE investment_profiles ADD COLUMN is_comparison_a BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE investment_profiles ADD COLUMN is_comparison_b BOOLEAN NOT NULL DEFAULT 0",
        # look_through_ticker: public-market equivalent used only for HHI look-through
        "ALTER TABLE assets ADD COLUMN look_through_ticker VARCHAR(20)",
    ]
    # Data migrations: set look_through_ticker for known institutional fund classes
    # whose holdings mirror a publicly-listed fund on Yahoo Finance.
    data_migrations = [
        # Fidelity Contrafund Pool Cl F (CUSIP 31617E745) mirrors FCNTX
        ("UPDATE assets SET look_through_ticker = 'FCNTX' "
         "WHERE cusip = '31617E745' AND look_through_ticker IS NULL"),
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore
        for stmt in data_migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def get_db_session(
    factory: sessionmaker[Session] | None = None,
) -> Generator[Session, None, None]:
    """Context manager for a database session with auto-commit/rollback."""
    if factory is None:
        engine = create_db_engine()
        factory = get_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Module-level convenience
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_db(cfg: Settings | None = None) -> tuple[Engine, sessionmaker[Session]]:
    """Initialize the database engine and session factory (singleton)."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_db_engine(cfg)
        create_tables(_engine)
        _session_factory = get_session_factory(_engine)
    return _engine, _session_factory


def get_engine() -> Engine:
    if _engine is None:
        init_db()
    return _engine  # type: ignore


def get_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        init_db()
    return _session_factory  # type: ignore

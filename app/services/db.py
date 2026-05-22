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


def create_db_engine(cfg: Settings | None = None) -> Engine:
    """Create and configure the SQLAlchemy engine."""
    cfg = cfg or settings

    # Ensure data directory exists
    db_path = Path(cfg.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(cfg.db_url, echo=False, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_tables(engine: Engine) -> None:
    """Create all tables from the ORM models."""
    Base.metadata.create_all(engine)


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

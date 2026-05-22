"""
FastAPI dependency injection for the Family Office.

Provides database sessions, settings, and service instances to route handlers.
"""

from typing import Generator

from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.services.db import get_factory


def get_settings() -> Settings:
    return settings


def get_db() -> Generator[Session, None, None]:
    """Yield a database session, auto-closing on completion."""
    factory = get_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

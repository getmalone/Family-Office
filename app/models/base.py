"""
Base ORM classes and mixins for the Family Office models.

Provides the declarative base, timestamp tracking, and soft-delete support
used across all Family Office entities.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all Family Office models."""

    pass


class TimestampMixin:
    """Adds created_at and updated_at columns for audit trail."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SoftDeleteMixin:
    """Adds soft-delete capability. Records are never physically deleted."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

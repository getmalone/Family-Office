"""Report and compliance flag models for the Family Office."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class GeneratedReport(Base, TimestampMixin):
    """A generated report."""

    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(100), default="reporting_agent")


class ComplianceFlag(Base, TimestampMixin):
    """A compliance risk or issue detected by the system."""

    __tablename__ = "compliance_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    flag_type: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(2000))
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_entities.id"), nullable=True
    )
    related_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

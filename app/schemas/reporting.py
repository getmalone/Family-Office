"""Reporting and compliance Pydantic schemas."""

from datetime import date

from pydantic import BaseModel


class ReportRequest(BaseModel):
    report_type: str  # quarterly_summary, tax_gain_loss, balance_sheet, schedule_d
    period_start: date
    period_end: date
    entity_id: int | None = None


class ReportOut(BaseModel):
    id: int
    report_type: str
    title: str
    period_start: date
    period_end: date
    generated_by: str

    model_config = {"from_attributes": True}


class ComplianceFlagOut(BaseModel):
    id: int
    flag_type: str
    severity: str
    description: str
    is_resolved: bool

    model_config = {"from_attributes": True}

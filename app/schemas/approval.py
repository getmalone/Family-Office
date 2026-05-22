"""Approval workflow Pydantic schemas."""

from datetime import datetime
from pydantic import BaseModel


class ApprovalOut(BaseModel):
    id: int
    status: str
    requested_by: str
    action_type: str
    action_summary: str
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    approved: bool
    notes: str = ""

"""
Human-in-the-loop approval models for the Family Office.

Every material investment or tax election requires explicit human approval
before execution. The approval system provides a clear audit trail and
ensures the Principal maintains control over significant financial decisions.
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ApprovalStatusEnum(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalPolicy(Base):
    """
    Defines when human approval is required.

    Policies can be set for trade amounts, gifts, manual journal entries,
    tax elections, and other material decisions.
    """

    __tablename__ = "approval_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    trigger_type: Mapped[str] = mapped_column(String(100))  # trade_amount, gift, journal_manual, tax_election
    threshold_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    required_approver_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id"), nullable=True
    )
    auto_expire_hours: Mapped[int] = mapped_column(default=72)
    is_active: Mapped[bool] = mapped_column(default=True)


class Approval(Base, TimestampMixin):
    """
    A pending, approved, or rejected action requiring human sign-off.

    The action_payload stores the full details of the proposed action as JSON,
    enabling the Principal to review exactly what will happen before approving.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_policies.id"), nullable=True
    )
    status: Mapped[ApprovalStatusEnum] = mapped_column(
        String(20), default=ApprovalStatusEnum.PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(100))  # agent name or "user"
    action_type: Mapped[str] = mapped_column(String(100))  # trade, gift, journal_entry, tax_election
    action_summary: Mapped[str] = mapped_column(String(1000))
    action_payload: Mapped[str] = mapped_column(Text)  # JSON blob
    approver_member_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id"), nullable=True
    )
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # LangGraph thread ID for resuming the paused graph
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Relationships
    policy: Mapped["ApprovalPolicy | None"] = relationship()

"""
Approval workflow service for the Family Office.

Implements human-in-the-loop controls for every material investment or tax election.
The Principal maintains ultimate authority over all significant financial decisions.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.approval import Approval, ApprovalPolicy, ApprovalStatusEnum
from app.schemas.approval import ApprovalDecision


class ApprovalService:
    """
    Manages the human-in-the-loop approval workflow.

    Checks policies to determine if an action requires approval, creates
    pending approvals, and resolves them based on the Principal's decision.
    """

    def __init__(self, session: Session):
        self.session = session

    def check_needs_approval(
        self, action_type: str, amount: Decimal | None = None
    ) -> ApprovalPolicy | None:
        """Check if an action triggers an approval policy."""
        policies = (
            self.session.query(ApprovalPolicy)
            .filter(
                ApprovalPolicy.trigger_type == action_type,
                ApprovalPolicy.is_active == True,
            )
            .all()
        )

        for policy in policies:
            if policy.threshold_amount is None:
                return policy  # Always requires approval
            if amount is not None and amount >= policy.threshold_amount:
                return policy

        return None

    def create_approval(
        self,
        action_type: str,
        action_summary: str,
        action_payload: dict,
        requested_by: str = "system",
        policy_id: int | None = None,
        thread_id: str | None = None,
    ) -> Approval:
        """Create a pending approval request."""
        policy = None
        if policy_id:
            policy = self.session.get(ApprovalPolicy, policy_id)

        expire_hours = policy.auto_expire_hours if policy else 72

        approval = Approval(
            policy_id=policy_id,
            status=ApprovalStatusEnum.PENDING,
            requested_by=requested_by,
            action_type=action_type,
            action_summary=action_summary,
            action_payload=json.dumps(action_payload, default=str),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=expire_hours),
            langgraph_thread_id=thread_id,
        )
        self.session.add(approval)
        self.session.flush()
        return approval

    def decide(self, approval_id: int, decision: ApprovalDecision) -> Approval:
        """Approve or reject a pending approval."""
        approval = self.session.get(Approval, approval_id)
        if not approval:
            raise ValueError(f"Approval {approval_id} not found")
        if approval.status != ApprovalStatusEnum.PENDING:
            raise ValueError(f"Approval {approval_id} is already {approval.status}")

        approval.status = (
            ApprovalStatusEnum.APPROVED if decision.approved else ApprovalStatusEnum.REJECTED
        )
        approval.decision_at = datetime.now(timezone.utc)
        approval.decision_notes = decision.notes
        self.session.flush()
        return approval

    def get_pending(self) -> list[Approval]:
        """Get all pending approvals."""
        return (
            self.session.query(Approval)
            .filter(Approval.status == ApprovalStatusEnum.PENDING)
            .order_by(Approval.created_at.desc())
            .all()
        )

    def get_history(self, limit: int = 50) -> list[Approval]:
        """Get resolved approvals."""
        return (
            self.session.query(Approval)
            .filter(Approval.status != ApprovalStatusEnum.PENDING)
            .order_by(Approval.decision_at.desc())
            .limit(limit)
            .all()
        )

    def expire_stale(self) -> int:
        """Expire approvals that have passed their expiration time."""
        now = datetime.now(timezone.utc)
        stale = (
            self.session.query(Approval)
            .filter(
                Approval.status == ApprovalStatusEnum.PENDING,
                Approval.expires_at < now,
            )
            .all()
        )
        for approval in stale:
            approval.status = ApprovalStatusEnum.EXPIRED
            approval.decision_at = now
        self.session.flush()
        return len(stale)

    def seed_default_policies(self) -> None:
        """Create default approval policies for the Family Office."""
        defaults = [
            ("Large Trade", "trade_amount", Decimal("50000"), 72),
            ("All Gifts", "gift", None, 168),
            ("Tax Election", "tax_election", None, 168),
            ("Manual Journal Entry", "journal_manual", Decimal("10000"), 72),
        ]
        for name, trigger, threshold, hours in defaults:
            existing = (
                self.session.query(ApprovalPolicy)
                .filter(ApprovalPolicy.name == name)
                .first()
            )
            if not existing:
                self.session.add(
                    ApprovalPolicy(
                        name=name,
                        trigger_type=trigger,
                        threshold_amount=threshold,
                        auto_expire_hours=hours,
                    )
                )
        self.session.flush()

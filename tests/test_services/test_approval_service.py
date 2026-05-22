"""Tests for the Approval Service."""

from decimal import Decimal

from app.models.approval import ApprovalStatusEnum
from app.schemas.approval import ApprovalDecision
from app.services.approval_service import ApprovalService


def test_check_needs_approval_under_threshold(seeded_session):
    """Trade under threshold does not need approval."""
    svc = ApprovalService(seeded_session)
    policy = svc.check_needs_approval("trade_amount", Decimal("10000"))
    assert policy is None


def test_check_needs_approval_over_threshold(seeded_session):
    """Trade over $50,000 threshold requires approval."""
    svc = ApprovalService(seeded_session)
    policy = svc.check_needs_approval("trade_amount", Decimal("75000"))
    assert policy is not None
    assert policy.name == "Large Trade"


def test_create_and_decide_approval(seeded_session):
    """Create an approval, then approve it."""
    svc = ApprovalService(seeded_session)

    approval = svc.create_approval(
        action_type="trade",
        action_summary="Buy 1000 shares of VOO",
        action_payload={"test": True},
        requested_by="portfolio_agent",
    )
    assert approval.status == ApprovalStatusEnum.PENDING

    # Decide
    result = svc.decide(approval.id, ApprovalDecision(approved=True, notes="Looks good"))
    assert result.status == ApprovalStatusEnum.APPROVED
    assert result.decision_notes == "Looks good"


def test_get_pending(seeded_session):
    """Get pending approvals returns only pending ones."""
    svc = ApprovalService(seeded_session)

    svc.create_approval("trade", "Test 1", {"a": 1}, "agent")
    svc.create_approval("trade", "Test 2", {"a": 2}, "agent")

    pending = svc.get_pending()
    assert len(pending) == 2

    # Approve one
    svc.decide(pending[0].id, ApprovalDecision(approved=True))
    assert len(svc.get_pending()) == 1

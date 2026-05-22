"""
Approval tools — enable agents to request and check human-in-the-loop approvals.
"""

from langchain_core.tools import tool


def create_approval_tools(session_factory):
    """Create approval tools with injected session factory."""

    @tool
    def request_approval(
        action_type: str,
        action_summary: str,
        requested_by: str = "agent",
    ) -> str:
        """
        Request human approval for a material action.
        Returns the approval ID. The action will be paused until approved.
        """
        from app.services.approval_service import ApprovalService

        with session_factory() as session:
            svc = ApprovalService(session)
            approval = svc.create_approval(
                action_type=action_type,
                action_summary=action_summary,
                action_payload={"summary": action_summary},
                requested_by=requested_by,
            )
            session.commit()
            return f"Approval requested (ID: {approval.id}). Waiting for human decision."

    @tool
    def check_approval_status(approval_id: int) -> str:
        """Check the status of a pending approval."""
        from app.models.approval import Approval
        from app.services.approval_service import ApprovalService

        with session_factory() as session:
            approval = session.get(Approval, approval_id)
            if not approval:
                return f"Approval {approval_id} not found"
            return f"Approval {approval_id}: {approval.status.value}"

    @tool
    def get_pending_approvals() -> str:
        """Get all pending approvals waiting for human decision."""
        from app.services.approval_service import ApprovalService
        import json

        with session_factory() as session:
            svc = ApprovalService(session)
            pending = svc.get_pending()
            return json.dumps([
                {
                    "id": a.id,
                    "action_type": a.action_type,
                    "action_summary": a.action_summary,
                    "requested_by": a.requested_by,
                    "status": a.status.value,
                }
                for a in pending
            ])

    return [request_approval, check_approval_status, get_pending_approvals]

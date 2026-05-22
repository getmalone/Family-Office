"""
Approval routes for the Family Office dashboard.

Implements the human-in-the-loop approval workflow for material decisions.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.schemas.approval import ApprovalDecision
from app.services.approval_service import ApprovalService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
def pending_approvals(request: Request, db: Session = Depends(get_db)):
    """Show pending approval queue."""
    svc = ApprovalService(db)
    svc.expire_stale()
    pending = svc.get_pending()

    # Parse action_payload JSON for display
    for approval in pending:
        try:
            approval._parsed_payload = json.loads(approval.action_payload)
        except (json.JSONDecodeError, TypeError):
            approval._parsed_payload = {}

    return templates.TemplateResponse(
        "approvals/pending.html",
        {"request": request, "pending": pending, "page_title": "Pending Approvals"},
    )


@router.post("/{approval_id}/decide")
def decide_approval(
    approval_id: int,
    approved: bool = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Approve or reject a pending approval."""
    svc = ApprovalService(db)
    decision = ApprovalDecision(approved=approved, notes=notes)
    approval = svc.decide(approval_id, decision)

    # If approved, execute the pending action
    if approval.status.value == "approved":
        try:
            payload = json.loads(approval.action_payload)
            if approval.action_type == "trade":
                _execute_approved_trade(db, payload)
        except Exception:
            pass  # Log and continue

    return RedirectResponse(url="/approvals/", status_code=303)


@router.get("/history")
def approval_history(request: Request, db: Session = Depends(get_db)):
    """Show resolved approvals."""
    svc = ApprovalService(db)
    history = svc.get_history()

    return templates.TemplateResponse(
        "approvals/history.html",
        {"request": request, "history": history, "page_title": "Approval History"},
    )


def _execute_approved_trade(db: Session, payload: dict) -> None:
    """Execute a trade that was approved via the approval workflow."""
    from decimal import Decimal
    from app.services.portfolio_service import PortfolioService
    from app.services.accounting_service import AccountingService

    svc = PortfolioService(db)
    acct_svc = AccountingService(db)

    action = payload.get("action", "buy")
    account_id = payload["account_id"]
    asset_id = payload["asset_id"]
    quantity = Decimal(str(payload["quantity"]))
    price = Decimal(str(payload["price"]))
    notes = payload.get("notes", "Approved trade")

    if action == "buy":
        txn = svc.execute_buy(account_id, asset_id, quantity, price, notes=notes)
        acct_svc.auto_journal_for_trade(txn)
    elif action == "sell":
        txn, _ = svc.execute_sell(account_id, asset_id, quantity, price, notes=notes)
        acct_svc.auto_journal_for_trade(txn)

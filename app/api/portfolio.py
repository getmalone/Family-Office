"""
Portfolio routes for the Family Office dashboard.

Provides portfolio overview, position detail, and trade execution endpoints.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.asset import Asset, AssetClassEnum
from app.models.account import Account, AccountTypeEnum
from app.models.family import FamilyMember, FamilyEntity
from app.services.approval_service import ApprovalService
from app.services.accounting_service import AccountingService
from app.services.market_data import MarketDataService
from app.services.portfolio_service import PortfolioService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/")
def portfolio_overview(request: Request, db: Session = Depends(get_db)):
    """Show all portfolio positions across accounts."""
    svc = PortfolioService(db)
    summary = svc.get_summary()
    accounts = db.query(Account).filter(Account.is_active == True).all()

    # Build a compact text snapshot for the AI advisor context
    import json
    holdings_for_ai = [
        {
            "symbol": h.symbol or h.name,
            "account": h.account_name,
            "asset_class": h.asset_class,
            "market_value": round(float(h.market_value), 2),
            "weight_pct": round(float(h.weight_pct), 1),
            "unrealized_pct": round(float(h.unrealized_pct), 1) if hasattr(h, "unrealized_pct") else None,
            "unrealized_gl": round(float(h.unrealized_gain_loss), 2),
        }
        for h in summary.holdings
    ]
    portfolio_context = (
        f"Total AUM: ${float(summary.total_market_value):,.0f}\n"
        f"Total Cost Basis: ${float(summary.total_cost_basis):,.0f}\n"
        f"Unrealized P&L: ${float(summary.total_unrealized_gain_loss):,.0f}\n\n"
        f"Asset Allocation:\n" +
        "\n".join(f"  {k.replace('_',' ').title()}: {v}%" for k, v in summary.allocation.items()) +
        f"\n\nHoldings ({len(holdings_for_ai)} positions):\n" +
        json.dumps(holdings_for_ai, indent=2)
    )

    return templates.TemplateResponse(
        "portfolio/overview.html",
        {
            "request": request,
            "summary": summary,
            "accounts": accounts,
            "portfolio_context": portfolio_context,
            "page_title": "Portfolio",
        },
    )


@router.get("/accounts-view")
def accounts_card_view(request: Request, db: Session = Depends(get_db)):
    """Account cards view — per-account breakdown with allocation and top holdings."""
    svc = PortfolioService(db)
    cards = svc.get_account_cards()
    total_mv = sum(c["market_value"] for c in cards)
    total_gl = sum(c["unrealized_gl"] for c in cards)
    return templates.TemplateResponse(
        "portfolio/accounts_overview.html",
        {
            "request": request,
            "cards": cards,
            "total_market_value": total_mv,
            "total_unrealized_gl": total_gl,
            "total_unrealized_pct": round(total_gl / (total_mv - total_gl) * 100, 2)
            if (total_mv - total_gl) > 0 else 0.0,
            "page_title": "Accounts",
        },
    )


@router.get("/trends")
def trends_view(request: Request, db: Session = Depends(get_db)):
    """Trends and charts — portfolio history, allocation, movers."""
    svc = PortfolioService(db)
    history = svc.get_portfolio_history(days=120)
    movers = svc.get_top_movers(limit=10)
    return templates.TemplateResponse(
        "portfolio/trends.html",
        {
            "request": request,
            "history": history,
            "movers": movers,
            "page_title": "Trends",
        },
    )


@router.get("/trade")
def trade_form(request: Request, db: Session = Depends(get_db)):
    """Show the trade entry form."""
    accounts = db.query(Account).filter(Account.is_active == True).all()
    assets = db.query(Asset).order_by(Asset.symbol).all()

    return templates.TemplateResponse(
        "portfolio/trade_form.html",
        {
            "request": request,
            "accounts": accounts,
            "assets": assets,
            "page_title": "New Trade",
        },
    )


@router.post("/trade")
def execute_trade(
    request: Request,
    account_id: int = Form(...),
    asset_id: int = Form(...),
    action: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Execute a buy or sell trade."""
    svc = PortfolioService(db)
    acct_svc = AccountingService(db)
    approval_svc = ApprovalService(db)

    qty = Decimal(str(quantity))
    ppu = Decimal(str(price))
    total = qty * ppu

    # Check if approval is needed
    policy = approval_svc.check_needs_approval("trade_amount", total)
    if policy:
        approval = approval_svc.create_approval(
            action_type="trade",
            action_summary=f"{action.upper()} {quantity} units of asset #{asset_id} at ${price}",
            action_payload={
                "account_id": account_id,
                "asset_id": asset_id,
                "action": action,
                "quantity": quantity,
                "price": price,
                "notes": notes,
            },
            requested_by="user",
            policy_id=policy.id,
        )
        return RedirectResponse(url="/approvals/", status_code=303)

    if action == "buy":
        txn = svc.execute_buy(account_id, asset_id, qty, ppu, notes=notes)
        acct_svc.auto_journal_for_trade(txn)
    elif action == "sell":
        txn, _ = svc.execute_sell(account_id, asset_id, qty, ppu, notes=notes)
        acct_svc.auto_journal_for_trade(txn)

    return RedirectResponse(url="/portfolio/", status_code=303)


@router.get("/edit/{asset_id}/{account_id}")
def edit_position_form(
    request: Request,
    asset_id: int,
    account_id: int,
    db: Session = Depends(get_db),
):
    """Show the edit form for a specific position."""
    svc = PortfolioService(db)
    position = svc.get_position_detail(asset_id, account_id)
    if not position:
        return RedirectResponse(url="/portfolio/", status_code=303)

    return templates.TemplateResponse(
        "portfolio/edit_position.html",
        {
            "request": request,
            "position": position,
            "page_title": f"Edit {position['symbol'] or position['name']}",
        },
    )


@router.post("/update")
def update_position(
    request: Request,
    asset_id: int = Form(...),
    account_id: int = Form(...),
    quantity: float = Form(...),
    cost_basis_per_unit: float = Form(...),
    db: Session = Depends(get_db),
):
    """Update an existing position's quantity and cost basis."""
    svc = PortfolioService(db)
    svc.update_position(
        asset_id=asset_id,
        account_id=account_id,
        new_quantity=Decimal(str(quantity)),
        new_cost_basis_per_unit=Decimal(str(cost_basis_per_unit)),
    )
    return RedirectResponse(url="/portfolio/", status_code=303)


@router.post("/delete/{asset_id}/{account_id}")
def delete_position(
    request: Request,
    asset_id: int,
    account_id: int,
    db: Session = Depends(get_db),
):
    """Delete (close) a position."""
    svc = PortfolioService(db)
    svc.delete_position(asset_id, account_id)
    return RedirectResponse(url="/portfolio/", status_code=303)


# ── Account Management ─────────────────────────────────────────────


@router.get("/accounts")
def accounts_list(request: Request, db: Session = Depends(get_db)):
    """List all accounts with add/edit/delete controls."""
    accounts = db.query(Account).order_by(Account.is_active.desc(), Account.name).all()
    members = db.query(FamilyMember).filter(FamilyMember.is_active == True).all()
    entities = db.query(FamilyEntity).filter(FamilyEntity.is_active == True).all()
    account_types = [(t.value, t.name.replace("_", " ").title()) for t in AccountTypeEnum]

    return templates.TemplateResponse(
        "portfolio/accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "members": members,
            "entities": entities,
            "account_types": account_types,
            "page_title": "Manage Accounts",
        },
    )


@router.post("/accounts/add")
def add_account(
    request: Request,
    name: str = Form(...),
    account_type: str = Form(...),
    institution: str = Form(""),
    is_taxable: bool = Form(False),
    primary_owner_id: int = Form(None),
    tax_entity_id: int = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new account."""
    acct = Account(
        name=name,
        account_type=account_type,
        institution=institution or None,
        is_taxable=is_taxable,
        primary_owner_id=primary_owner_id if primary_owner_id else None,
        tax_entity_id=tax_entity_id if tax_entity_id else None,
    )
    db.add(acct)
    db.flush()
    return RedirectResponse(url="/portfolio/accounts", status_code=303)


@router.get("/accounts/edit/{account_id}")
def edit_account_form(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    """Show edit form for an account."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/portfolio/accounts", status_code=303)

    members = db.query(FamilyMember).filter(FamilyMember.is_active == True).all()
    entities = db.query(FamilyEntity).filter(FamilyEntity.is_active == True).all()
    account_types = [(t.value, t.name.replace("_", " ").title()) for t in AccountTypeEnum]

    return templates.TemplateResponse(
        "portfolio/edit_account.html",
        {
            "request": request,
            "account": account,
            "members": members,
            "entities": entities,
            "account_types": account_types,
            "page_title": f"Edit {account.name}",
        },
    )


@router.post("/accounts/update/{account_id}")
def update_account(
    request: Request,
    account_id: int,
    name: str = Form(...),
    account_type: str = Form(...),
    institution: str = Form(""),
    is_taxable: bool = Form(False),
    primary_owner_id: int = Form(None),
    tax_entity_id: int = Form(None),
    db: Session = Depends(get_db),
):
    """Update an existing account."""
    account = db.get(Account, account_id)
    if not account:
        return RedirectResponse(url="/portfolio/accounts", status_code=303)

    account.name = name
    account.account_type = account_type
    account.institution = institution or None
    account.is_taxable = is_taxable
    account.primary_owner_id = primary_owner_id if primary_owner_id else None
    account.tax_entity_id = tax_entity_id if tax_entity_id else None
    db.flush()
    return RedirectResponse(url="/portfolio/accounts", status_code=303)


@router.post("/accounts/delete/{account_id}")
def delete_account(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
):
    """Deactivate an account (soft delete)."""
    account = db.get(Account, account_id)
    if account:
        account.is_active = False
    return RedirectResponse(url="/portfolio/accounts", status_code=303)


# ── Asset Management ───────────────────────────────────────────────


def _asset_class_choices() -> list[tuple[str, str]]:
    return [(t.value, t.name.replace("_", " ").title()) for t in AssetClassEnum]


@router.get("/assets")
def assets_list(request: Request, db: Session = Depends(get_db)):
    """List all assets with add/edit controls."""
    assets = db.query(Asset).order_by(Asset.symbol, Asset.name).all()

    return templates.TemplateResponse(
        "portfolio/assets.html",
        {
            "request": request,
            "assets": assets,
            "asset_classes": _asset_class_choices(),
            "page_title": "Manage Assets",
        },
    )


@router.post("/assets/add")
def add_asset(
    request: Request,
    symbol: str = Form(""),
    name: str = Form(...),
    asset_class: str = Form(...),
    is_publicly_traded: bool = Form(False),
    sector: str = Form(""),
    cusip: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a new asset."""
    asset = Asset(
        symbol=symbol.upper().strip() or None,
        name=name.strip(),
        asset_class=asset_class,
        is_publicly_traded=is_publicly_traded,
        sector=sector.strip() or None,
        cusip=cusip.strip() or None,
    )
    db.add(asset)
    db.flush()
    return RedirectResponse(url="/portfolio/assets", status_code=303)


@router.get("/assets/edit/{asset_id}")
def edit_asset_form(
    request: Request,
    asset_id: int,
    db: Session = Depends(get_db),
):
    """Show edit form for an asset."""
    asset = db.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/portfolio/assets", status_code=303)

    return templates.TemplateResponse(
        "portfolio/edit_asset.html",
        {
            "request": request,
            "asset": asset,
            "asset_classes": _asset_class_choices(),
            "page_title": f"Edit {asset.symbol or asset.name}",
        },
    )


@router.post("/assets/update/{asset_id}")
def update_asset(
    request: Request,
    asset_id: int,
    symbol: str = Form(""),
    name: str = Form(...),
    asset_class: str = Form(...),
    is_publicly_traded: bool = Form(False),
    sector: str = Form(""),
    cusip: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update an existing asset."""
    asset = db.get(Asset, asset_id)
    if not asset:
        return RedirectResponse(url="/portfolio/assets", status_code=303)

    asset.symbol = symbol.upper().strip() or None
    asset.name = name.strip()
    asset.asset_class = asset_class
    asset.is_publicly_traded = is_publicly_traded
    asset.sector = sector.strip() or None
    asset.cusip = cusip.strip() or None
    db.flush()
    return RedirectResponse(url="/portfolio/assets", status_code=303)

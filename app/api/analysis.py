"""
Financial analysis routes for the Family Office dashboard.

Provides risk metrics, investment profile management, drift/rebalancing analysis,
and Monte Carlo simulation endpoints.
"""

import json
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.analysis_service import AnalysisService
from app.services.morning_brief_service import MorningBriefService

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/morning")
def morning_brief(
    request: Request,
    refresh: int = Query(0),
    db: Session = Depends(get_db),
):
    """Daily brief — morning context or evening report, cached per market session."""
    from app.services.snapshot_service import SnapshotService
    snap = SnapshotService(db)

    if refresh:
        # Prices were already updated by the JS two-step (POST /prices/refresh
        # then GET /analysis/morning?refresh=1).  All we do here is bust the cache.
        from app.services.morning_brief_service import invalidate_brief_cache
        invalidate_brief_cache()
    else:
        # Opportunistic capture on open — kicked to a background thread. Doing it
        # inline meant a quote fetch per held symbol plus writes DURING the render:
        # slow, and it fought the backfill for the SQLite write lock (a lock or
        # duplicate error left this request's session un-committable → 500).
        from app.services.backfill_worker import ensure_capture
        ensure_capture()

    svc = MorningBriefService(db)
    try:
        brief = svc.get_brief(force_refresh=bool(refresh))
    except Exception as e:
        brief = {
            "as_of": "today",
            "session": "AM",
            "report_title": "Daily Brief",
            "next_update": "",
            "fetched_at": "",
            "indices": [], "sectors": [], "holdings_impact": [],
            "portfolio_day": {"total_mv": 0, "day_chg": 0, "day_chg_pct": 0, "up": True},
            "narrative": [f"Unable to load market data: {e}"],
            "market_regime": None,
        }

    try:
        port_change = snap.change_since_previous()
        readings = snap.recent_readings()
    except Exception:
        port_change, readings = {"available": False}, []

    # page_title drives the sub-nav active state — keep it stable
    return templates.TemplateResponse(
        "analysis/morning.html",
        {"request": request, "brief": brief, "port_change": port_change,
         "readings": readings, "page_title": "Morning Brief"},
    )


@router.get("/")
def analysis_overview(request: Request, db: Session = Depends(get_db)):
    """Main analysis page: risk summary + allocation drift."""
    svc = AnalysisService(db)

    try:
        risk = svc.get_risk_metrics(lookback_days=252)
    except Exception:
        risk = svc._empty_risk_metrics(252, 0.05)

    profile = svc.get_active_profile()
    drift = None
    if profile:
        try:
            drift = svc.get_drift_analysis()
        except Exception:
            pass

    return templates.TemplateResponse(
        "analysis/overview.html",
        {
            "request": request,
            "risk": risk,
            "profile": profile,
            "drift": drift,
            "page_title": "Analysis",
        },
    )


@router.get("/risk")
def risk_detail(
    request: Request,
    lookback: int = Query(252, ge=30, le=1260),
    db: Session = Depends(get_db),
):
    """Detailed risk analysis page."""
    svc = AnalysisService(db)
    try:
        risk = svc.get_risk_metrics(lookback_days=lookback)
    except Exception:
        risk = svc._empty_risk_metrics(lookback, 0.05)

    return templates.TemplateResponse(
        "analysis/risk.html",
        {
            "request": request,
            "risk": risk,
            "lookback": lookback,
            "page_title": "Risk Analysis",
        },
    )


@router.get("/profiles")
def profiles_page(request: Request, db: Session = Depends(get_db)):
    """Investment profiles management page."""
    svc = AnalysisService(db)
    profiles = svc.list_profiles()

    return templates.TemplateResponse(
        "analysis/profiles.html",
        {
            "request": request,
            "profiles": profiles,
            "page_title": "Investment Profiles",
        },
    )


@router.post("/profiles")
def create_profile(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    us_equity: float = Form(0),
    intl_equity: float = Form(0),
    fixed_income: float = Form(0),
    real_estate: float = Form(0),
    commodity: float = Form(0),
    cash: float = Form(0),
    crypto: float = Form(0),
    private_equity: float = Form(0),
    venture_capital: float = Form(0),
    hedge_fund: float = Form(0),
    alternative: float = Form(0),
    other: float = Form(0),
    db: Session = Depends(get_db),
):
    """Create a custom investment profile."""
    svc = AnalysisService(db)
    allocs = {
        "us_equity": Decimal(str(us_equity)),
        "intl_equity": Decimal(str(intl_equity)),
        "fixed_income": Decimal(str(fixed_income)),
        "real_estate": Decimal(str(real_estate)),
        "commodity": Decimal(str(commodity)),
        "cash": Decimal(str(cash)),
        "crypto": Decimal(str(crypto)),
        "private_equity": Decimal(str(private_equity)),
        "venture_capital": Decimal(str(venture_capital)),
        "hedge_fund": Decimal(str(hedge_fund)),
        "alternative": Decimal(str(alternative)),
        "other": Decimal(str(other)),
    }
    # Remove zero allocations
    allocs = {k: v for k, v in allocs.items() if v > 0}
    svc.create_custom_profile(name, allocs, description)
    return RedirectResponse(url="/analysis/profiles", status_code=303)


@router.post("/profiles/{profile_id}/activate")
def activate_profile(profile_id: int, db: Session = Depends(get_db)):
    """Set a profile as active."""
    svc = AnalysisService(db)
    svc.set_active_profile(profile_id)
    return RedirectResponse(url="/analysis/profiles", status_code=303)


@router.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    """Delete a custom profile."""
    svc = AnalysisService(db)
    svc.delete_profile(profile_id)
    return RedirectResponse(url="/analysis/profiles", status_code=303)


@router.post("/profiles/{profile_id}/set-comparison-a")
def set_comparison_a(profile_id: int, db: Session = Depends(get_db)):
    """Select a profile as Monte Carlo Comparison A (left column)."""
    svc = AnalysisService(db)
    svc.set_comparison_profile(profile_id, "a")
    return RedirectResponse(url="/analysis/profiles", status_code=303)


@router.post("/profiles/{profile_id}/set-comparison-b")
def set_comparison_b(profile_id: int, db: Session = Depends(get_db)):
    """Select a profile as Monte Carlo Comparison B (right column)."""
    svc = AnalysisService(db)
    svc.set_comparison_profile(profile_id, "b")
    return RedirectResponse(url="/analysis/profiles", status_code=303)


@router.get("/rebalance")
def rebalance_page(request: Request, db: Session = Depends(get_db)):
    """Show drift analysis and rebalancing recommendations."""
    svc = AnalysisService(db)

    drift = None
    error = None
    try:
        drift = svc.get_drift_analysis()
    except ValueError as e:
        error = str(e)

    return templates.TemplateResponse(
        "analysis/rebalance.html",
        {
            "request": request,
            "drift": drift,
            "error": error,
            "page_title": "Rebalance",
        },
    )


def _parse_budget(raw: str | None) -> list[dict] | None:
    """Budget items from the form's JSON field: [{"name", "monthly", "fixed"}]."""
    if not raw or not raw.strip():
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        return None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            monthly = round(max(0.0, float(it.get("monthly") or 0)), 2)
        except (TypeError, ValueError):
            continue
        name = str(it.get("name") or "").strip()[:80] or "Spending"
        out.append({"name": name, "monthly": monthly, "fixed": bool(it.get("fixed"))})
    return out


def _apply_buffer(items: list[dict] | None, buffer_pct: float) -> list[dict] | None:
    """Scale every budget line by the buffer (e.g. 10% → ×1.10)."""
    if items is None:
        return None
    f = 1 + max(0.0, buffer_pct) / 100
    return [{**it, "monthly": round(it["monthly"] * f, 2)} for it in items]


@router.get("/monte-carlo")
def monte_carlo_page(
    request: Request,
    years: int = Query(10, ge=0, le=50),
    simulations: int = Query(1000, ge=100, le=10000),
    goal: str | None = Query(None),
    monthly_contribution: str | None = Query(None),
    withdrawal_rate: str | None = Query(None),
    withdrawal_years: int = Query(20, ge=1, le=50),
    regime_override: str | None = Query(None),
    spending_pattern: str = Query("constant"),
    smile_slow_pct: float = Query(80.0, ge=0, le=100),
    smile_no_pct: float = Query(65.0, ge=0, le=100),
    current_age: int | None = Query(None, ge=18, le=100),
    retirement_age: int | None = Query(None, ge=30, le=100),
    ssa_claiming_age: int | None = Query(None, ge=62, le=70),
    ssa_monthly: str | None = Query(None),  # est. monthly benefit at Full Retirement Age (67)
    fra_age: int = Query(67, ge=66, le=67),         # primary's full retirement age (pre-1960 birth → 66)
    spouse_monthly: str | None = Query(None),       # spouse's est. monthly benefit at their FRA
    spouse_claiming_age: int | None = Query(None, ge=62, le=70),
    spouse_current_age: int | None = Query(None, ge=18, le=100),
    spouse_fra_age: int = Query(67, ge=66, le=67),
    income_bridge: bool = Query(False),     # model account-type withdrawal sequencing + taxes
    deferred_tax_rate: float = Query(18.0, ge=0, le=50),  # effective % tax on tax-deferred withdrawals
    taxable_tax_rate: float = Query(10.0, ge=0, le=50),   # effective % cap-gains drag on taxable
    bucket_taxable: str | None = Query(None),       # manual override of today's taxable $ balance
    bucket_traditional: str | None = Query(None),   # manual override of tax-deferred $ balance
    bucket_roth: str | None = Query(None),          # manual override of tax-free (Roth) $ balance
    spending_mode: str | None = Query(None),        # "rate" (withdrawal %) or "budget"; None = last used
    budget_items: str | None = Query(None),         # JSON list of {name, monthly, fixed}
    inflation_rate: float = Query(2.5, ge=0, le=15),  # % per year; results shown in today's dollars
    budget_buffer: float | None = Query(None, ge=0, le=100),  # % cushion on top of the budget lines
    reinvest_surplus: bool | None = Query(None),    # income above spending goes back into the portfolio
    db: Session = Depends(get_db),
):
    """Monte Carlo simulation page."""
    from app.services import app_settings
    svc = AnalysisService(db)

    # Ages override the Accumulation Years dropdown (the service does the same),
    # so echo the derived value back to keep the form in sync with the engine.
    if current_age is not None and retirement_age is not None:
        years = max(0, retirement_age - current_age)

    def _to_decimal(s: str | None) -> Decimal | None:
        if s and s.strip():
            try:
                return Decimal(s.strip())
            except Exception:
                pass
        return None

    goal_decimal    = _to_decimal(goal)
    goal_display    = float(goal_decimal) if goal_decimal else None

    contrib_decimal = _to_decimal(monthly_contribution) or Decimal("0")
    contrib_display = float(contrib_decimal) if contrib_decimal else None

    wdraw_decimal   = _to_decimal(withdrawal_rate) or Decimal("0")
    wdraw_display   = float(wdraw_decimal) if wdraw_decimal else None
    # Only run distribution phase when a nonzero rate was supplied
    # Budget: the submitted one is remembered; otherwise use the saved one.
    budget = _parse_budget(budget_items)
    # The form always sends spending_mode, so its presence means "submitted":
    # remember the mode, the surplus choice, and (in budget mode) the budget.
    if spending_mode is not None:
        app_settings.set_setting(db, "mc_spending_mode", spending_mode)
        app_settings.set_setting(db, "mc_reinvest_surplus", "1" if reinvest_surplus else "")
        if budget is not None:
            app_settings.set_setting(db, "mc_budget_items", json.dumps(budget))
        if budget_buffer is not None:
            app_settings.set_setting(db, "mc_budget_buffer", str(budget_buffer))
        db.commit()
    else:
        spending_mode = app_settings.get_setting(db, "mc_spending_mode") or "rate"
        reinvest_surplus = bool(app_settings.get_setting(db, "mc_reinvest_surplus"))
    reinvest_surplus = bool(reinvest_surplus)
    if budget is None:
        budget = _parse_budget(app_settings.get_setting(db, "mc_budget_items")) or []
    if budget_buffer is None:
        try:
            budget_buffer = float(app_settings.get_setting(db, "mc_budget_buffer") or 10)
        except ValueError:
            budget_buffer = 10.0
    budget_mode = spending_mode == "budget" and any(it["monthly"] > 0 for it in budget)
    wdraw_years_int = withdrawal_years if (wdraw_decimal > 0 or budget_mode) else 0

    ssa_monthly_decimal = _to_decimal(ssa_monthly) or Decimal("0")
    ssa_monthly_display = float(ssa_monthly_decimal) if ssa_monthly_decimal else None

    spouse_monthly_decimal = _to_decimal(spouse_monthly) or Decimal("0")
    spouse_monthly_display = float(spouse_monthly_decimal) if spouse_monthly_decimal else None

    # Manual bucket overrides — when any is supplied, use them verbatim (handy
    # before the real accounts are loaded); otherwise the service auto-derives
    # the buckets from the account ledger.
    bucket_overrides: dict[str, float] | None = None
    if income_bridge and any(v is not None and v.strip() for v in (bucket_taxable, bucket_traditional, bucket_roth)):
        bucket_overrides = {
            "taxable": float(_to_decimal(bucket_taxable) or 0),
            "traditional": float(_to_decimal(bucket_traditional) or 0),
            "roth": float(_to_decimal(bucket_roth) or 0),
        }

    # Normalise override: treat "auto"/None the same way (service auto-detects)
    regime_ov = regime_override.lower().strip() if regime_override else None
    if regime_ov == "auto":
        regime_ov = None

    result = svc.run_monte_carlo(
        years=years,
        num_simulations=simulations,
        goal_amount=goal_decimal,
        monthly_contribution=contrib_decimal,
        withdrawal_rate=wdraw_decimal,
        withdrawal_years=wdraw_years_int,
        regime_override=regime_ov,
        spending_pattern=spending_pattern,
        smile_slow_pct=smile_slow_pct,
        smile_no_pct=smile_no_pct,
        current_age=current_age,
        retirement_age=retirement_age,
        ssa_claiming_age=ssa_claiming_age,
        ssa_monthly_fra=ssa_monthly_decimal,
        fra_age=fra_age,
        spouse_monthly_fra=spouse_monthly_decimal,
        spouse_claiming_age=spouse_claiming_age,
        spouse_current_age=spouse_current_age,
        spouse_fra_age=spouse_fra_age,
        income_bridge=income_bridge,
        bucket_balances=bucket_overrides,
        deferred_tax_rate=deferred_tax_rate / 100.0,
        taxable_tax_rate=taxable_tax_rate / 100.0,
        inflation_rate=inflation_rate / 100.0,
        budget_items=_apply_buffer(budget, budget_buffer) if budget_mode else None,
        reinvest_surplus=reinvest_surplus,
    )

    return templates.TemplateResponse(
        "analysis/monte_carlo.html",
        {
            "request": request,
            "result": result,
            "years": years,
            "simulations": simulations,
            "goal": goal_display,
            "monthly_contribution": contrib_display,
            "withdrawal_rate": wdraw_display,
            "withdrawal_years": withdrawal_years,
            "regime_override": regime_override or "auto",
            "spending_pattern": spending_pattern,
            "smile_slow_pct": smile_slow_pct,
            "smile_no_pct": smile_no_pct,
            "current_age": current_age,
            "retirement_age": retirement_age,
            "ssa_claiming_age": ssa_claiming_age,
            "ssa_monthly": ssa_monthly_display,
            "fra_age": fra_age,
            "spouse_monthly": spouse_monthly_display,
            "spouse_claiming_age": spouse_claiming_age,
            "spouse_current_age": spouse_current_age,
            "spouse_fra_age": spouse_fra_age,
            "income_bridge": income_bridge,
            "deferred_tax_rate": deferred_tax_rate,
            "taxable_tax_rate": taxable_tax_rate,
            "bucket_taxable": bucket_taxable,
            "bucket_traditional": bucket_traditional,
            "bucket_roth": bucket_roth,
            "spending_mode": "budget" if spending_mode == "budget" else "rate",
            "budget_items": budget,
            "budget_buffer": int(budget_buffer) if float(budget_buffer).is_integer() else budget_buffer,
            "inflation_rate": inflation_rate,
            "reinvest_surplus": reinvest_surplus,
            "page_title": "Monte Carlo",
        },
    )


@router.get("/api/expense-baseline")
def api_expense_baseline(months: int = Query(12, ge=1, le=36), db: Session = Depends(get_db)):
    """Average monthly spending per tracked expense category (budget pre-fill)."""
    return JSONResponse(content={"items": AnalysisService(db).expense_baseline(months)})


@router.get("/api/correlation")
def api_correlation(
    lookback_days: int = Query(252, ge=60, le=1260),
    db: Session = Depends(get_db),
):
    """Return pairwise correlation matrix as JSON for heatmap rendering."""
    svc = AnalysisService(db)
    result = svc.get_correlation_matrix(lookback_days=lookback_days)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/api/returns")
def api_returns(db: Session = Depends(get_db)):
    """Return time-weighted return metrics as JSON."""
    svc = AnalysisService(db)
    result = svc.get_return_metrics()
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/api/max-spending")
def api_max_spending(
    target: float = Query(85.0, ge=1, le=99),
    simulations: int = Query(2000, ge=100, le=10000),
    monthly_contribution: str | None = Query(None),
    withdrawal_years: int = Query(30, ge=1, le=50),
    regime_override: str | None = Query(None),
    spending_pattern: str = Query("constant"),
    smile_slow_pct: float = Query(80.0, ge=0, le=100),
    smile_no_pct: float = Query(65.0, ge=0, le=100),
    years: int = Query(10, ge=0, le=50),
    current_age: int | None = Query(None, ge=18, le=100),
    retirement_age: int | None = Query(None, ge=30, le=100),
    ssa_claiming_age: int | None = Query(None, ge=62, le=70),
    ssa_monthly: str | None = Query(None),
    fra_age: int = Query(67, ge=66, le=67),
    spouse_monthly: str | None = Query(None),
    spouse_claiming_age: int | None = Query(None, ge=62, le=70),
    spouse_current_age: int | None = Query(None, ge=18, le=100),
    spouse_fra_age: int = Query(67, ge=66, le=67),
    income_bridge: bool = Query(False),
    deferred_tax_rate: float = Query(18.0, ge=0, le=50),
    taxable_tax_rate: float = Query(10.0, ge=0, le=50),
    bucket_taxable: str | None = Query(None),
    bucket_traditional: str | None = Query(None),
    bucket_roth: str | None = Query(None),
    spending_mode: str = Query("rate"),
    budget_items: str | None = Query(None),
    inflation_rate: float = Query(2.5, ge=0, le=15),
    budget_buffer: float = Query(10.0, ge=0, le=100),
    reinvest_surplus: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Highest sustainable spending that keeps survival ≥ target (%) — JSON."""
    svc = AnalysisService(db)

    def _to_decimal(s: str | None) -> Decimal | None:
        if s and s.strip():
            try:
                return Decimal(s.strip())
            except Exception:
                pass
        return None

    regime_ov = regime_override.lower().strip() if regime_override else None
    if regime_ov == "auto":
        regime_ov = None

    # Same manual-bucket rule as the page: any override → use them verbatim.
    bucket_overrides: dict[str, float] | None = None
    if income_bridge and any(v is not None and v.strip() for v in (bucket_taxable, bucket_traditional, bucket_roth)):
        bucket_overrides = {
            "taxable": float(_to_decimal(bucket_taxable) or 0),
            "traditional": float(_to_decimal(bucket_traditional) or 0),
            "roth": float(_to_decimal(bucket_roth) or 0),
        }

    result = svc.solve_max_spending(
        years=years,
        num_simulations=simulations,
        monthly_contribution=_to_decimal(monthly_contribution) or Decimal("0"),
        withdrawal_years=withdrawal_years,
        regime_override=regime_ov,
        spending_pattern=spending_pattern,
        smile_slow_pct=smile_slow_pct,
        smile_no_pct=smile_no_pct,
        current_age=current_age,
        retirement_age=retirement_age,
        ssa_claiming_age=ssa_claiming_age,
        ssa_monthly_fra=_to_decimal(ssa_monthly) or Decimal("0"),
        fra_age=fra_age,
        spouse_monthly_fra=_to_decimal(spouse_monthly) or Decimal("0"),
        spouse_claiming_age=spouse_claiming_age,
        spouse_current_age=spouse_current_age,
        spouse_fra_age=spouse_fra_age,
        income_bridge=income_bridge,
        bucket_balances=bucket_overrides,
        deferred_tax_rate=deferred_tax_rate / 100.0,
        taxable_tax_rate=taxable_tax_rate / 100.0,
        inflation_rate=inflation_rate / 100.0,
        budget_items=(_apply_buffer(_parse_budget(budget_items), budget_buffer)
                      if spending_mode == "budget" else None),
        reinvest_surplus=reinvest_surplus,
        target_survival=target,
    )
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/api/monte-carlo")
def api_monte_carlo(
    years: int = Query(10, ge=0, le=50),
    simulations: int = Query(1000, ge=100, le=10000),
    goal: str | None = Query(None),
    monthly_contribution: str | None = Query(None),
    withdrawal_rate: str | None = Query(None),
    withdrawal_years: int = Query(20, ge=1, le=50),
    regime_override: str | None = Query(None),
    spending_pattern: str = Query("constant"),
    smile_slow_pct: float = Query(80.0, ge=0, le=100),
    smile_no_pct: float = Query(65.0, ge=0, le=100),
    current_age: int | None = Query(None, ge=18, le=100),
    retirement_age: int | None = Query(None, ge=30, le=100),
    ssa_claiming_age: int | None = Query(None, ge=62, le=70),
    ssa_monthly: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Return Monte Carlo results as JSON for Chart.js rendering."""
    svc = AnalysisService(db)

    def _to_decimal(s: str | None) -> Decimal | None:
        if s and s.strip():
            try:
                return Decimal(s.strip())
            except Exception:
                pass
        return None

    goal_decimal    = _to_decimal(goal)
    contrib_decimal = _to_decimal(monthly_contribution) or Decimal("0")
    wdraw_decimal   = _to_decimal(withdrawal_rate) or Decimal("0")
    wdraw_years     = withdrawal_years if wdraw_decimal > 0 else 0
    ssa_monthly_decimal = _to_decimal(ssa_monthly) or Decimal("0")

    regime_ov = regime_override.lower().strip() if regime_override else None
    if regime_ov == "auto":
        regime_ov = None

    result = svc.run_monte_carlo(
        years=years,
        num_simulations=simulations,
        goal_amount=goal_decimal,
        monthly_contribution=contrib_decimal,
        withdrawal_rate=wdraw_decimal,
        withdrawal_years=wdraw_years,
        regime_override=regime_ov,
        spending_pattern=spending_pattern,
        smile_slow_pct=smile_slow_pct,
        smile_no_pct=smile_no_pct,
        current_age=current_age,
        retirement_age=retirement_age,
        ssa_claiming_age=ssa_claiming_age,
        ssa_monthly_fra=ssa_monthly_decimal,
    )
    return JSONResponse(content=result.model_dump(mode="json"))

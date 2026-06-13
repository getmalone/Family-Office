"""Tests for the system investment profiles (incl. new strategy-based ones)."""

from app.models.asset import AssetClassEnum
from app.models.investment_profile import InvestmentProfile, RiskToleranceEnum
from app.services.analysis_service import DEFAULT_PROFILES, AnalysisService

VALID_CLASSES = {c.value for c in AssetClassEnum}
VALID_RISK = {r.value for r in RiskToleranceEnum}


def test_every_profile_allocation_sums_to_100():
    for p in DEFAULT_PROFILES:
        total = sum(p["allocations"].values())
        assert total == 100, f"{p['name']} allocations sum to {total}, not 100"


def test_profiles_use_valid_classes_and_risk_levels():
    for p in DEFAULT_PROFILES:
        assert p["risk_tolerance"] in VALID_RISK, p["name"]
        for cls in p["allocations"]:
            assert cls in VALID_CLASSES, f"{p['name']}: unknown asset class {cls}"


def test_expected_strategy_profiles_present():
    names = {p["name"] for p in DEFAULT_PROFILES}
    for expected in (
        "Income", "60/40 Classic", "Three-Fund Index",
        "All-Weather (Risk Parity)", "Permanent Portfolio", "Endowment (Yale-Style)",
    ):
        assert expected in names, f"missing strategy profile: {expected}"


def test_exactly_one_default_active():
    active = [p for p in DEFAULT_PROFILES if p.get("is_active")]
    assert len(active) == 1 and active[0]["name"] == "Balanced"


def test_seed_is_idempotent_and_single_active(session):
    svc = AnalysisService(session)
    svc.seed_default_profiles()
    svc.seed_default_profiles()  # second run must not duplicate
    rows = session.query(InvestmentProfile).filter(InvestmentProfile.is_system == True).all()  # noqa: E712
    assert len(rows) == len(DEFAULT_PROFILES)
    assert sum(1 for r in rows if r.is_active) == 1

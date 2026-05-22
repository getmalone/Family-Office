"""
Estate Agent tools — LangChain tool wrappers around EstateService.

These tools enable the Estate & Family Governance Agent to track ownership,
record gifts, simulate GRATs and SLATs, and maintain the family dashboard.
"""

from datetime import date
from decimal import Decimal

from langchain_core.tools import tool


def create_estate_tools(session_factory):
    """Create estate tools with injected session factory."""

    @tool
    def get_ownership_tree() -> str:
        """Get the family ownership tree showing each member's economic interest."""
        from app.services.estate_service import EstateService

        with session_factory() as session:
            svc = EstateService(session)
            tree = svc.get_ownership_tree()
            import json
            return json.dumps([n.model_dump(mode="json") for n in tree])

    @tool
    def get_gift_history(tax_year: int | None = None) -> str:
        """Get gift history, optionally filtered by tax year."""
        from app.services.estate_service import EstateService

        with session_factory() as session:
            svc = EstateService(session)
            gifts = svc.get_gifts(tax_year=tax_year)
            import json
            return json.dumps([g.model_dump(mode="json") for g in gifts])

    @tool
    def simulate_grat(
        name: str,
        grantor_member_id: int,
        initial_funding: float,
        term_years: int = 2,
        section_7520_rate: float = 5.0,
    ) -> str:
        """Simulate a GRAT with multiple growth scenarios to estimate tax-free wealth transfer."""
        from app.services.estate_service import EstateService
        from app.schemas.estate import GRATParams

        with session_factory() as session:
            svc = EstateService(session)
            params = GRATParams(
                name=name,
                grantor_member_id=grantor_member_id,
                initial_funding=Decimal(str(initial_funding)),
                term_years=term_years,
                section_7520_rate=Decimal(str(section_7520_rate)),
            )
            results = svc.simulate_grat(params)
            session.commit()
            import json
            return json.dumps([r.model_dump(mode="json") for r in results])

    @tool
    def get_annual_exclusion_remaining(
        donor_id: int, recipient_id: int, tax_year: int
    ) -> str:
        """Check remaining annual gift exclusion for a donor-recipient pair."""
        from app.services.estate_service import EstateService

        with session_factory() as session:
            svc = EstateService(session)
            remaining = svc.get_annual_exclusion_remaining(donor_id, recipient_id, tax_year)
            return f"Remaining annual exclusion: ${remaining}"

    @tool
    def get_lifetime_exemption_remaining(donor_id: int) -> str:
        """Check remaining lifetime gift/estate tax exemption."""
        from app.services.estate_service import EstateService

        with session_factory() as session:
            svc = EstateService(session)
            remaining = svc.get_lifetime_exemption_remaining(donor_id)
            return f"Remaining lifetime exemption: ${remaining:,.2f}"

    return [
        get_ownership_tree,
        get_gift_history,
        simulate_grat,
        get_annual_exclusion_remaining,
        get_lifetime_exemption_remaining,
    ]

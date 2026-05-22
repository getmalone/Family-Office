"""
Tax Optimization Agent tools — LangChain tool wrappers around TaxService.

These tools enable the Tax Optimization Agent to run tax-loss harvesting scans,
identify optimal tax elections, track deductible Family Office expenses, and
generate real-time 'tax alpha' reports.
"""

from decimal import Decimal

from langchain_core.tools import tool


def create_tax_tools(session_factory):
    """Create tax tools with injected session factory."""

    @tool
    def find_harvest_candidates(
        min_loss: float = 100.0,
        account_id: int | None = None,
    ) -> str:
        """Find positions with unrealized losses eligible for tax-loss harvesting."""
        from app.services.tax_service import TaxService

        with session_factory() as session:
            svc = TaxService(session)
            candidates = svc.find_harvest_candidates(
                min_loss=Decimal(str(min_loss)),
                account_id=account_id,
            )
            import json
            return json.dumps([c.model_dump(mode="json") for c in candidates])

    @tool
    def get_tax_summary(tax_year: int | None = None) -> str:
        """Get comprehensive tax summary with gains, losses, income, and estimated tax."""
        from app.services.tax_service import TaxService

        with session_factory() as session:
            svc = TaxService(session)
            summary = svc.get_tax_summary(tax_year)
            return summary.model_dump_json()

    @tool
    def simulate_tax_election(election_type: str) -> str:
        """
        Simulate the impact of a tax election.
        Supported types: 'cost_basis_method', 'section_475'
        """
        from app.services.tax_service import TaxService

        with session_factory() as session:
            svc = TaxService(session)
            results = svc.simulate_election(election_type)
            import json
            return json.dumps([r.model_dump(mode="json") for r in results])

    return [find_harvest_candidates, get_tax_summary, simulate_tax_election]

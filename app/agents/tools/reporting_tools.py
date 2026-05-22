"""
Reporting Agent tools — LangChain tool wrappers around ReportingService.

These tools enable the Reporting & Compliance Agent to generate investor-grade
reports and flag compliance risks.
"""

from datetime import date

from langchain_core.tools import tool


def create_reporting_tools(session_factory):
    """Create reporting tools with injected session factory."""

    @tool
    def generate_quarterly_report(period_start: str, period_end: str) -> str:
        """Generate a quarterly summary report for the given date range (YYYY-MM-DD)."""
        from app.services.reporting_service import ReportingService

        with session_factory() as session:
            svc = ReportingService(session)
            report = svc.generate_quarterly_report(
                date.fromisoformat(period_start),
                date.fromisoformat(period_end),
            )
            session.commit()
            return f"Report generated: {report.title} (ID: {report.id})"

    @tool
    def generate_schedule_d(tax_year: int) -> str:
        """Generate Schedule D / Form 8949 data for tax filing."""
        from app.services.reporting_service import ReportingService

        with session_factory() as session:
            svc = ReportingService(session)
            report = svc.generate_schedule_d_data(tax_year)
            session.commit()
            return f"Schedule D generated: {report.title} (ID: {report.id})"

    @tool
    def check_compliance() -> str:
        """Run compliance checks and return any flags found."""
        from app.services.reporting_service import ReportingService

        with session_factory() as session:
            svc = ReportingService(session)
            flags = svc.scan_compliance()
            session.commit()
            if not flags:
                return "No new compliance issues found."
            import json
            return json.dumps([
                {"type": f.flag_type, "severity": f.severity, "description": f.description}
                for f in flags
            ])

    @tool
    def get_compliance_flags() -> str:
        """Get all active compliance flags."""
        from app.services.reporting_service import ReportingService

        with session_factory() as session:
            svc = ReportingService(session)
            flags = svc.get_compliance_flags()
            import json
            return json.dumps([f.model_dump(mode="json") for f in flags])

    return [generate_quarterly_report, generate_schedule_d, check_compliance, get_compliance_flags]

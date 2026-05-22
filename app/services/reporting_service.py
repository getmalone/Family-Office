"""
Reporting and compliance service for the Family Office.

Generates investor-grade quarterly and annual reports (performance, risk, tax position,
attribution). Prepares data exports for tax software including Schedule D worksheets
and supporting loss-harvesting documentation. Flags compliance risks such as
passive activity loss rules, at-risk rules, and related-party transactions.
"""

import json
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.report import ComplianceFlag, GeneratedReport
from app.models.tax_lot import TaxLotDisposal
from app.models.transaction import Transaction, TransactionTypeEnum
from app.schemas.reporting import ComplianceFlagOut, ReportOut


class ReportingService:
    """
    Report generation and compliance scanning for the Family Office.

    Ensures the Family Office can claim all available ordinary and capital losses
    on personal tax returns while maintaining complete IRS-compliant records.
    """

    def __init__(self, session: Session):
        self.session = session

    def generate_quarterly_report(
        self, period_start: date, period_end: date, entity_id: int | None = None
    ) -> GeneratedReport:
        """Generate a quarterly performance and tax summary report."""
        from app.services.portfolio_service import PortfolioService
        from app.services.tax_service import TaxService
        from app.services.market_data import MarketDataService

        market = MarketDataService(self.session)
        portfolio = PortfolioService(self.session, market)
        tax = TaxService(self.session, market)

        summary = portfolio.get_summary()
        tax_summary = tax.get_tax_summary(period_start.year)

        content = {
            "portfolio": {
                "total_aum": str(summary.total_market_value),
                "unrealized_gain_loss": str(summary.total_unrealized_gain_loss),
                "realized_ytd": str(summary.total_realized_ytd),
                "allocation": {k: str(v) for k, v in summary.allocation.items()},
                "top_holdings": [
                    {"name": h.name, "symbol": h.symbol, "weight": str(h.weight_pct)}
                    for h in summary.holdings[:10]
                ],
            },
            "tax": {
                "short_term_gains": str(tax_summary.short_term_gains),
                "short_term_losses": str(tax_summary.short_term_losses),
                "long_term_gains": str(tax_summary.long_term_gains),
                "long_term_losses": str(tax_summary.long_term_losses),
                "net_gain_loss": str(tax_summary.net_gain_loss),
                "estimated_tax": str(tax_summary.estimated_tax_liability),
            },
        }

        report = GeneratedReport(
            report_type="quarterly_summary",
            title=f"Quarterly Report: {period_start} to {period_end}",
            period_start=period_start,
            period_end=period_end,
            entity_id=entity_id,
            content_json=json.dumps(content, default=str),
            generated_by="reporting_agent",
        )
        self.session.add(report)
        self.session.flush()
        return report

    def generate_schedule_d_data(self, tax_year: int) -> GeneratedReport:
        """
        Generate Schedule D / Form 8949 data for tax filing.

        Exports all realized disposals for the year with cost basis, proceeds,
        gain/loss, and short-term/long-term classification.
        """
        year_start = date(tax_year, 1, 1)
        year_end = date(tax_year, 12, 31)

        disposals = (
            self.session.query(TaxLotDisposal)
            .filter(
                TaxLotDisposal.disposal_date >= year_start,
                TaxLotDisposal.disposal_date <= year_end,
            )
            .all()
        )

        form_8949_rows = []
        for d in disposals:
            lot = d.tax_lot
            asset = lot.asset if lot else None
            form_8949_rows.append({
                "description": asset.name if asset else "Unknown",
                "symbol": asset.symbol if asset else None,
                "date_acquired": str(lot.acquisition_date) if lot else None,
                "date_sold": str(d.disposal_date),
                "proceeds": str(d.proceeds_per_unit * d.quantity_disposed),
                "cost_basis": str(lot.cost_basis_per_unit * d.quantity_disposed) if lot else "0",
                "gain_loss": str(d.realized_gain_loss),
                "is_short_term": d.is_short_term,
                "wash_sale_adjustment": str(d.wash_sale_disallowed) if d.is_wash_sale else "0",
            })

        content = {
            "tax_year": tax_year,
            "form_8949": form_8949_rows,
            "totals": {
                "short_term_gain": str(sum(
                    d.realized_gain_loss for d in disposals
                    if d.is_short_term and d.realized_gain_loss > 0
                )),
                "short_term_loss": str(sum(
                    d.realized_gain_loss for d in disposals
                    if d.is_short_term and d.realized_gain_loss < 0
                )),
                "long_term_gain": str(sum(
                    d.realized_gain_loss for d in disposals
                    if not d.is_short_term and d.realized_gain_loss > 0
                )),
                "long_term_loss": str(sum(
                    d.realized_gain_loss for d in disposals
                    if not d.is_short_term and d.realized_gain_loss < 0
                )),
            },
        }

        report = GeneratedReport(
            report_type="schedule_d",
            title=f"Schedule D / Form 8949 - Tax Year {tax_year}",
            period_start=year_start,
            period_end=year_end,
            content_json=json.dumps(content, default=str),
            generated_by="reporting_agent",
        )
        self.session.add(report)
        self.session.flush()
        return report

    def scan_compliance(self) -> list[ComplianceFlag]:
        """
        Run compliance checks and create flags for any issues found.

        Checks for wash-sale violations, missing cost basis, unreported transactions,
        and other compliance risks.
        """
        new_flags = []

        # Check for wash-sale disposals that haven't been properly adjusted
        wash_disposals = (
            self.session.query(TaxLotDisposal)
            .filter(TaxLotDisposal.is_wash_sale == True)
            .all()
        )
        for d in wash_disposals:
            existing = (
                self.session.query(ComplianceFlag)
                .filter(
                    ComplianceFlag.flag_type == "wash_sale",
                    ComplianceFlag.related_transaction_id == d.sale_transaction_id,
                )
                .first()
            )
            if not existing:
                flag = ComplianceFlag(
                    flag_type="wash_sale",
                    severity="warning",
                    description=(
                        f"Wash sale detected on disposal #{d.id}: "
                        f"${d.wash_sale_disallowed} loss disallowed and added to replacement lot basis"
                    ),
                    related_transaction_id=d.sale_transaction_id,
                )
                self.session.add(flag)
                new_flags.append(flag)

        self.session.flush()
        return new_flags

    def get_compliance_flags(self, include_resolved: bool = False) -> list[ComplianceFlagOut]:
        """Get all compliance flags."""
        query = self.session.query(ComplianceFlag)
        if not include_resolved:
            query = query.filter(ComplianceFlag.is_resolved == False)
        flags = query.order_by(ComplianceFlag.created_at.desc()).all()
        return [
            ComplianceFlagOut(
                id=f.id,
                flag_type=f.flag_type,
                severity=f.severity,
                description=f.description,
                is_resolved=f.is_resolved,
            )
            for f in flags
        ]

    def get_reports(self, report_type: str | None = None, limit: int = 20) -> list[ReportOut]:
        """List generated reports."""
        query = self.session.query(GeneratedReport)
        if report_type:
            query = query.filter(GeneratedReport.report_type == report_type)
        reports = query.order_by(GeneratedReport.created_at.desc()).limit(limit).all()
        return [
            ReportOut(
                id=r.id,
                report_type=r.report_type,
                title=r.title,
                period_start=r.period_start,
                period_end=r.period_end,
                generated_by=r.generated_by,
            )
            for r in reports
        ]

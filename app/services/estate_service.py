"""
Estate planning and family governance service for the Family Office.

Supports coordinated estate planning, gifting, charitable giving, and generational
wealth transfer while always prioritizing the long-term financial security of the
Principal, spouse, and children. Simulates GRATs, SLATs, and other planning tools.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate import Gift, GRATSimulation
from app.models.family import FamilyEntity, FamilyMember, Ownership
from app.schemas.estate import GiftCreate, GiftOut, GRATParams, GRATResult, OwnershipNode


# 2026 gift tax exclusion amounts (approximate)
ANNUAL_EXCLUSION = Decimal("18000")
LIFETIME_EXEMPTION = Decimal("13610000")  # 2026 projected


class EstateService:
    """
    Estate planning operations for the Family Office.

    Tracks ownership percentages, gift history, and provides GRAT simulation
    to enable tax-efficient generational wealth transfer.
    """

    def __init__(self, session: Session):
        self.session = session

    def record_gift(self, data: GiftCreate) -> Gift:
        """
        Record a gift and allocate between annual exclusion and lifetime exemption.

        Tracks all details required for Form 709 (Gift Tax Return) including
        fair market value, donor cost basis for in-kind gifts, and proper allocation.
        """
        year = data.gift_date.year

        # Charitable gifts are fully deductible — they never consume annual exclusion
        # or lifetime exemption, and no Form 709 is required for pure charitable gifts.
        if data.is_charitable:
            annual_applied = Decimal("0")
            lifetime_applied = Decimal("0")
            excess = Decimal("0")
        else:
            # Calculate used annual exclusion for this specific donor→recipient pair this year
            used = self._get_annual_exclusion_used(
                data.donor_member_id, data.recipient_member_id, year
            )
            remaining_annual = max(Decimal("0"), ANNUAL_EXCLUSION - used)
            annual_applied = min(data.fair_market_value, remaining_annual)
            excess = data.fair_market_value - annual_applied

            # Consume lifetime exemption for any amount above the annual exclusion
            lifetime_used = self._get_lifetime_exemption_used(data.donor_member_id)
            remaining_lifetime = max(Decimal("0"), LIFETIME_EXEMPTION - lifetime_used)
            lifetime_applied = min(excess, remaining_lifetime)

        gift = Gift(
            donor_member_id=data.donor_member_id,
            recipient_member_id=data.recipient_member_id,
            recipient_entity_id=data.recipient_entity_id,
            recipient_external=data.recipient_external,
            asset_id=data.asset_id,
            gift_date=data.gift_date,
            fair_market_value=data.fair_market_value,
            donor_cost_basis=data.donor_cost_basis,
            annual_exclusion_applied=annual_applied,
            lifetime_exemption_applied=lifetime_applied,
            tax_year=year,
            is_charitable=data.is_charitable,
            notes=data.notes,
            form_709_filed=excess > 0,  # Form 709 required only if non-charitable excess
        )
        self.session.add(gift)
        self.session.flush()
        return gift

    def get_gifts(
        self, tax_year: int | None = None, donor_id: int | None = None
    ) -> list[GiftOut]:
        """List gifts with donor/recipient names."""
        query = self.session.query(Gift)
        if tax_year:
            query = query.filter(Gift.tax_year == tax_year)
        if donor_id:
            query = query.filter(Gift.donor_member_id == donor_id)

        gifts = query.order_by(Gift.gift_date.desc()).all()
        results = []
        for g in gifts:
            donor = self.session.get(FamilyMember, g.donor_member_id)
            recipient_name = "External"
            if g.recipient_member_id:
                r = self.session.get(FamilyMember, g.recipient_member_id)
                recipient_name = r.full_name if r else "Unknown"
            elif g.recipient_entity_id:
                e = self.session.get(FamilyEntity, g.recipient_entity_id)
                recipient_name = e.name if e else "Unknown"
            elif g.recipient_external:
                recipient_name = g.recipient_external

            results.append(
                GiftOut(
                    id=g.id,
                    donor_name=donor.full_name if donor else "Unknown",
                    recipient_name=recipient_name,
                    gift_date=g.gift_date,
                    fair_market_value=g.fair_market_value,
                    annual_exclusion_applied=g.annual_exclusion_applied,
                    lifetime_exemption_applied=g.lifetime_exemption_applied,
                    is_charitable=g.is_charitable,
                )
            )
        return results

    def simulate_grat(self, params: GRATParams) -> list[GRATResult]:
        """
        Simulate a Grantor Retained Annuity Trust (GRAT).

        Models the transfer of wealth to beneficiaries at reduced or zero gift tax cost.
        The remainder passes to beneficiaries tax-free if trust assets outperform
        the Section 7520 hurdle rate.
        """
        results = []
        hurdle = params.section_7520_rate / 100

        # Calculate annuity payment for a zeroed-out GRAT
        # Annuity = Funding * (hurdle / (1 - (1 + hurdle)^-term))
        if hurdle > 0:
            annuity = params.initial_funding * (
                hurdle / (1 - (1 + hurdle) ** (-params.term_years))
            )
        else:
            annuity = params.initial_funding / params.term_years

        for growth_rate in params.assumed_growth_rates:
            gr = growth_rate / 100
            balance = params.initial_funding
            total_annuity_paid = Decimal("0")

            for year in range(1, params.term_years + 1):
                balance = balance * (1 + gr)
                balance -= annuity
                total_annuity_paid += annuity

            remainder = max(Decimal("0"), balance)
            # Tax savings: estate tax rate * remainder value
            estate_tax_rate = Decimal("0.40")
            tax_savings = remainder * estate_tax_rate
            effective_transfer = (
                (remainder / params.initial_funding * 100) if params.initial_funding else Decimal("0")
            )

            results.append(
                GRATResult(
                    growth_rate=growth_rate,
                    annuity_payment=round(annuity, 2),
                    remainder_value=round(remainder, 2),
                    tax_savings_estimate=round(tax_savings, 2),
                    effective_transfer_pct=round(effective_transfer, 2),
                )
            )

            # Save simulation to DB
            sim = GRATSimulation(
                name=params.name,
                grantor_member_id=params.grantor_member_id,
                initial_funding=params.initial_funding,
                term_years=params.term_years,
                section_7520_rate=params.section_7520_rate,
                assumed_growth_rate=growth_rate,
                annuity_payment=round(annuity, 2),
                remainder_value=round(remainder, 2),
                tax_savings_estimate=round(tax_savings, 2),
            )
            self.session.add(sim)

        self.session.flush()
        return results

    def get_ownership_tree(self) -> list[OwnershipNode]:
        """Build the family ownership tree showing each member's economic interest."""
        members = self.session.query(FamilyMember).filter(FamilyMember.is_active == True).all()

        nodes = []
        for member in members:
            ownerships = (
                self.session.query(Ownership)
                .filter(Ownership.owner_member_id == member.id, Ownership.end_date == None)
                .all()
            )
            children = []
            for own in ownerships:
                if own.account_id:
                    from app.models.account import Account

                    acct = self.session.get(Account, own.account_id)
                    if acct:
                        children.append(
                            OwnershipNode(
                                id=acct.id,
                                name=acct.name,
                                node_type="account",
                                ownership_pct=own.ownership_pct,
                            )
                        )

            nodes.append(
                OwnershipNode(
                    id=member.id,
                    name=member.full_name,
                    node_type="member",
                    ownership_pct=Decimal("100"),
                    children=children,
                )
            )
        return nodes

    def get_annual_exclusion_remaining(
        self, donor_id: int, recipient_id: int | None, tax_year: int
    ) -> Decimal:
        """Calculate remaining annual gift exclusion for a donor-recipient pair."""
        used = self._get_annual_exclusion_used(donor_id, recipient_id, tax_year)
        return max(Decimal("0"), ANNUAL_EXCLUSION - used)

    def get_lifetime_exemption_remaining(self, donor_id: int) -> Decimal:
        """Calculate remaining lifetime gift/estate exemption."""
        used = self._get_lifetime_exemption_used(donor_id)
        return max(Decimal("0"), LIFETIME_EXEMPTION - used)

    def _get_annual_exclusion_used(
        self, donor_id: int, recipient_id: int | None, year: int
    ) -> Decimal:
        # Annual exclusion is per-donor-per-recipient, so always filter to the specific recipient.
        # When recipient_id is None (entity/external gift) explicitly match NULL rows so we don't
        # accidentally sum exclusions used for *all* recipients of this donor in the year.
        query = self.session.query(func.coalesce(func.sum(Gift.annual_exclusion_applied), 0)).filter(
            Gift.donor_member_id == donor_id,
            Gift.tax_year == year,
            Gift.is_charitable == False,
        )
        if recipient_id is not None:
            query = query.filter(Gift.recipient_member_id == recipient_id)
        else:
            query = query.filter(Gift.recipient_member_id == None)  # noqa: E711
        return Decimal(str(query.scalar() or 0))

    def _get_lifetime_exemption_used(self, donor_id: int) -> Decimal:
        result = (
            self.session.query(func.coalesce(func.sum(Gift.lifetime_exemption_applied), 0))
            .filter(Gift.donor_member_id == donor_id)
            .scalar()
        )
        return Decimal(str(result or 0))

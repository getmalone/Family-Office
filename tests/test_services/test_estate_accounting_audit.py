"""Regression tests for the estate and accounting fixes from the calculation
audit (part D): year-specific gift-tax figures, a separate annual exclusion for
each outside recipient, balanced journal entries for sales with fees, and a
balance sheet that balances after realized gains.
"""

from datetime import date
from decimal import Decimal

from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum
from app.models.family import FamilyMember
from app.models.journal import ChartOfAccounts
from app.schemas.accounting import JournalEntryCreate
from app.schemas.estate import GiftCreate
from app.services.accounting_service import AccountingService
from app.services.estate_service import EstateService, annual_exclusion, lifetime_exemption
from app.services.portfolio_service import PortfolioService

D = Decimal


# ── Estate ──────────────────────────────────────────────────────────────────

def test_gift_tax_figures_by_year():
    assert annual_exclusion(2024) == D(18000)
    assert annual_exclusion(2025) == D(19000) and annual_exclusion(2026) == D(19000)
    assert lifetime_exemption(2024) == D(13_610_000)
    assert lifetime_exemption(2025) == D(13_990_000)
    assert lifetime_exemption(2026) == D(15_000_000)
    assert lifetime_exemption(2030) == D(15_000_000)            # latest known until updated


def _donor(session):
    m = FamilyMember(first_name="Alex", last_name="M", relationship="principal",
                     date_of_birth=date(1970, 1, 1))
    session.add(m)
    session.flush()
    return m


def test_each_outside_recipient_gets_own_exclusion(session):
    donor = _donor(session)
    svc = EstateService(session)
    ann = svc.record_gift(GiftCreate(donor_member_id=donor.id, recipient_external="Niece Ann",
                                     gift_date=date(2026, 3, 1), fair_market_value=D(19000)))
    bob = svc.record_gift(GiftCreate(donor_member_id=donor.id, recipient_external="Friend Bob",
                                     gift_date=date(2026, 3, 2), fair_market_value=D(19000)))
    assert ann.annual_exclusion_applied == D(19000)
    assert bob.annual_exclusion_applied == D(19000)             # was 0 (pooled with Ann)
    assert bob.lifetime_exemption_applied == D(0)
    again = svc.record_gift(GiftCreate(donor_member_id=donor.id, recipient_external=" niece ann ",
                                       gift_date=date(2026, 6, 1), fair_market_value=D(5000)))
    assert again.annual_exclusion_applied == D(0)               # same person, already used


def test_lifetime_exemption_uses_gift_year(session):
    donor = _donor(session)
    g = EstateService(session).record_gift(GiftCreate(
        donor_member_id=donor.id, recipient_external="Trust Beneficiary",
        gift_date=date(2026, 1, 15), fair_market_value=D(14_500_000)))
    assert g.lifetime_exemption_applied == D(14_481_000)        # all of it fits under $15M


# ── Accounting ──────────────────────────────────────────────────────────────

def _fund_and_trade(session, sell_fees=D(0)):
    acct = Account(name="B", account_type=AccountTypeEnum.BROKERAGE, institution="T", is_taxable=True)
    asset = Asset(symbol="XYZ", name="XYZ", asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add_all([acct, asset])
    session.flush()
    acc = AccountingService(session)
    acc.seed_chart_of_accounts()
    cash = session.query(ChartOfAccounts).filter_by(code="1010").one()
    equity = session.query(ChartOfAccounts).filter_by(code="3100").one()
    acc.create_journal_entry(JournalEntryCreate(
        entry_date=date(2026, 1, 2), description="Fund", reference="F1", source="test",
        lines=[{"coa_id": cash.id, "debit": D(10000), "credit": D(0)},
               {"coa_id": equity.id, "debit": D(0), "credit": D(10000)}]))
    ps = PortfolioService(session)
    acc.auto_journal_for_trade(ps.execute_buy(acct.id, asset.id, D(100), D(100),
                                              transaction_date=date(2026, 1, 5)))
    txn, _ = ps.execute_sell(acct.id, asset.id, D(100), D(120), fees=sell_fees,
                             transaction_date=date(2026, 2, 5))
    return acc, acc.auto_journal_for_trade(txn)


def test_sell_with_fees_journal_balances(session):
    _, entry = _fund_and_trade(session, sell_fees=D(10))         # used to raise "unbalanced"
    assert entry is not None


def test_balance_sheet_balances_after_gain(session):
    acc, _ = _fund_and_trade(session)
    bs = acc.get_balance_sheet(date(2026, 12, 31))
    assert bs.total_assets == D(12000)
    assert bs.total_assets == bs.total_liabilities + bs.total_equity   # was 12,000 vs 10,000

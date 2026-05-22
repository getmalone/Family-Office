"""
Test fixtures for the Family Office dashboard.

Provides an in-memory SQLite database and test client for API testing.
"""

import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient

# Override DB before importing app
os.environ["KFO_DB_PATH"] = ":memory:"
os.environ["KFO_DB_PASSPHRASE"] = "test"

from app.models.base import Base
from app.models.family import FamilyMember, FamilyEntity
from app.models.account import Account, AccountTypeEnum
from app.models.asset import Asset, AssetClassEnum, AssetPrice
from app.models.transaction import Transaction, TransactionTypeEnum
from app.models.tax_lot import TaxLot
from app.services.accounting_service import AccountingService
from app.services.approval_service import ApprovalService


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with all tables.

    Uses StaticPool to share the same in-memory DB across connections,
    which is required for SQLite in-memory databases in a multi-connection context.
    """
    import app.models  # noqa: F401
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine) -> Session:
    """Create a database session for testing."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sess = factory()
    yield sess
    sess.close()


@pytest.fixture
def seeded_session(session) -> Session:
    """Session with basic seed data for testing."""
    # Family members
    principal = FamilyMember(
        first_name="Alex", last_name="Morgan",
        relationship="principal", date_of_birth=date(1985, 6, 15),
    )
    spouse = FamilyMember(
        first_name="Jamie", last_name="Morgan",
        relationship="spouse", date_of_birth=date(1987, 3, 22),
    )
    session.add_all([principal, spouse])
    session.flush()

    # Entity
    llc = FamilyEntity(
        name="Morgan Family Holdings LLC",
        entity_type="llc", tax_election="partnership",
    )
    session.add(llc)
    session.flush()

    # Account
    brokerage = Account(
        name="Test Brokerage", account_type=AccountTypeEnum.BROKERAGE,
        institution="Test Broker", is_taxable=True,
        primary_owner_id=principal.id, tax_entity_id=llc.id,
    )
    session.add(brokerage)
    session.flush()

    # Assets
    voo = Asset(symbol="VOO", name="Vanguard S&P 500 ETF",
                asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    aapl = Asset(symbol="AAPL", name="Apple Inc.",
                 asset_class=AssetClassEnum.US_EQUITY, is_publicly_traded=True)
    session.add_all([voo, aapl])
    session.flush()

    # Prices
    today = date.today()
    session.add_all([
        AssetPrice(asset_id=voo.id, price_date=today, close_price=Decimal("520.50"), source="test"),
        AssetPrice(asset_id=aapl.id, price_date=today, close_price=Decimal("195.20"), source="test"),
    ])

    # Tax lot for VOO
    txn = Transaction(
        account_id=brokerage.id, asset_id=voo.id,
        transaction_type=TransactionTypeEnum.BUY,
        transaction_date=date(2023, 3, 15),
        quantity=Decimal("100"), price_per_unit=Decimal("450.00"),
        total_amount=Decimal("45000"), fees=Decimal("0"),
    )
    session.add(txn)
    session.flush()

    lot = TaxLot(
        account_id=brokerage.id, asset_id=voo.id,
        acquisition_date=date(2023, 3, 15),
        acquisition_transaction_id=txn.id,
        original_quantity=Decimal("100"), remaining_quantity=Decimal("100"),
        cost_basis_per_unit=Decimal("450.00"),
        original_cost_basis_per_unit=Decimal("450.00"),
    )
    session.add(lot)

    # Seed chart of accounts and approval policies
    acct_svc = AccountingService(session)
    acct_svc.seed_chart_of_accounts()
    approval_svc = ApprovalService(session)
    approval_svc.seed_default_policies()

    session.flush()
    return session


@pytest.fixture
def test_client(engine):
    """Create a FastAPI test client with the test database."""
    from app.services import db as db_module
    from app.models.base import Base as ModelBase

    # Create all tables on test engine
    ModelBase.metadata.create_all(engine)

    # Patch module-level singletons
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db_module._engine = engine
    db_module._session_factory = factory

    from app import main as main_module
    from app.dependencies import get_db

    def override_get_db():
        sess = factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    main_module.app.dependency_overrides[get_db] = override_get_db

    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        yield client

    main_module.app.dependency_overrides.clear()

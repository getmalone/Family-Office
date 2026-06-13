"""Tests for Alembic-based schema migrations (fresh / existing / encrypted)."""

from sqlalchemy import inspect

from app.config import settings
from app.services.db import create_db_engine, create_tables
from app.services.migrations import run_migrations


def _use(monkeypatch, path, passphrase=""):
    # env.py builds its engine from the global settings, so mutate that object.
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "db_passphrase", passphrase)


def test_fresh_db_creates_schema(tmp_path, monkeypatch):
    _use(monkeypatch, str(tmp_path / "fresh.db"))
    engine = create_db_engine(settings)
    assert run_migrations(engine, settings) == "upgraded"
    tables = set(inspect(engine).get_table_names())
    assert "alembic_version" in tables
    assert "investment_profiles" in tables


def test_existing_pre_alembic_db_is_stamped(tmp_path, monkeypatch):
    _use(monkeypatch, str(tmp_path / "existing.db"))
    engine = create_db_engine(settings)
    create_tables(engine)  # simulate a pre-Alembic database
    assert run_migrations(engine, settings) == "stamped"
    assert "alembic_version" in set(inspect(engine).get_table_names())


def test_migrations_idempotent(tmp_path, monkeypatch):
    _use(monkeypatch, str(tmp_path / "idem.db"))
    engine = create_db_engine(settings)
    run_migrations(engine, settings)
    assert run_migrations(engine, settings) == "current"


def test_encrypted_db_migrates(tmp_path, monkeypatch):
    path = str(tmp_path / "enc.db")
    _use(monkeypatch, path, passphrase="pw")
    engine = create_db_engine(settings)
    assert run_migrations(engine, settings) == "upgraded"
    with open(path, "rb") as f:
        assert not f.read(16).startswith(b"SQLite format 3")  # encrypted on disk

"""Tests for SQLCipher at-rest encryption and the db_admin tooling."""

import argparse
import sqlite3

import pytest
from sqlalchemy import text

from app.config import Settings
from app.services.db import create_db_engine, create_tables, is_db_encrypted


def _cfg(tmp_path, passphrase="", name="t.db"):
    # Explicit db_passphrase overrides the KFO_DB_PASSPHRASE the test env sets.
    return Settings(db_path=str(tmp_path / name), db_passphrase=passphrase)


def test_plaintext_when_no_passphrase(tmp_path):
    cfg = _cfg(tmp_path)
    assert is_db_encrypted(cfg) is False
    engine = create_db_engine(cfg)
    create_tables(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE x(a)"))
        c.execute(text("INSERT INTO x VALUES (1)"))
    engine.dispose()
    with open(cfg.db_path, "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")  # standard plaintext header


def test_encrypted_roundtrip_and_unreadable_on_disk(tmp_path):
    cfg = _cfg(tmp_path, "hunter2")
    assert is_db_encrypted(cfg) is True
    engine = create_db_engine(cfg)
    create_tables(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE secret(v)"))
        c.execute(text("INSERT INTO secret VALUES (42)"))
    with engine.connect() as c:
        assert c.execute(text("SELECT v FROM secret")).scalar() == 42
    engine.dispose()

    # On disk it must NOT look like a plaintext SQLite database…
    with open(cfg.db_path, "rb") as f:
        assert not f.read(16).startswith(b"SQLite format 3")
    # …and the stdlib (non-SQLCipher) driver cannot read it.
    con = sqlite3.connect(cfg.db_path)
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("SELECT name FROM sqlite_master").fetchall()
    con.close()


def test_wrong_passphrase_rejected(tmp_path):
    create_tables(create_db_engine(_cfg(tmp_path, "correct-key")))
    bad = create_db_engine(_cfg(tmp_path, "wrong-key"))
    with pytest.raises(Exception):
        with bad.connect() as c:
            c.execute(text("SELECT count(*) FROM sqlite_master")).scalar()
    bad.dispose()


def test_db_admin_encrypt_decrypt_roundtrip(tmp_path):
    from scripts import db_admin

    # Build a plaintext source DB with a row.
    plain = _cfg(tmp_path, name="plain.db")
    engine = create_db_engine(plain)
    create_tables(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE m(v)"))
        c.execute(text("INSERT INTO m VALUES ('hello')"))
    engine.dispose()

    enc = str(tmp_path / "enc.db")
    back = str(tmp_path / "back.db")

    db_admin.cmd_encrypt(argparse.Namespace(src=plain.db_path, dst=enc, passphrase="pw"))
    with open(enc, "rb") as f:
        assert not f.read(16).startswith(b"SQLite format 3")  # encrypted

    db_admin.cmd_decrypt(argparse.Namespace(src=enc, dst=back, passphrase="pw"))
    con = sqlite3.connect(back)  # plaintext again → stdlib can read it
    assert con.execute("SELECT v FROM m").fetchone()[0] == "hello"
    con.close()

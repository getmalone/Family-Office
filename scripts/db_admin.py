#!/usr/bin/env python
"""
Database administration for the Family Office: create, encrypt, decrypt.

Run with the project virtualenv, e.g.:

    # Create a fresh, empty, ENCRYPTED database ready to ship to a user's laptop
    ./.venv/bin/python scripts/db_admin.py create --path data/new.db --passphrase "s3cret"

    # Encrypt an existing plaintext database (makes a new encrypted copy)
    ./.venv/bin/python scripts/db_admin.py encrypt --src data/plain.db --dst data/enc.db --passphrase "s3cret"

    # Decrypt back to plaintext (e.g. for export / inspection)
    ./.venv/bin/python scripts/db_admin.py decrypt --src data/enc.db --dst data/plain.db --passphrase "s3cret"

Encryption uses SQLCipher (AES-256) via the cross-platform ``sqlcipher3-wheels``
package — the same engine the app uses at runtime when KFO_DB_PASSPHRASE is set.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running directly from the repo without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _esc(value: str) -> str:
    """Escape a string for safe inclusion in a single-quoted SQL literal."""
    return value.replace("'", "''")


def cmd_create(args: argparse.Namespace) -> None:
    """Create a fresh database with the full schema, optionally encrypted/seeded."""
    from app.config import Settings
    from app.services.db import create_tables, create_db_engine, get_session_factory

    path = args.path
    if os.path.exists(path):
        if not args.force:
            _die(f"{path} already exists (use --force to overwrite)")
        os.remove(path)

    cfg = Settings(db_path=path, db_passphrase=args.passphrase or "")
    engine = create_db_engine(cfg)
    create_tables(engine)

    if args.seed_demo:
        # seed() calls init_db() internally; point the module singletons at our
        # engine so it writes to this database (with the right key) instead of
        # creating its own from the default settings.
        from app.services import db as dbmod
        dbmod._engine = engine
        dbmod._session_factory = get_session_factory(engine)
        try:
            from scripts.seed_demo_data import seed  # type: ignore
            seed()
            print("  · seeded demo data")
        except Exception as exc:  # pragma: no cover - seeding is best-effort
            print(f"  · warning: could not seed demo data ({exc})")

    engine.dispose()
    kind = "encrypted (SQLCipher)" if args.passphrase else "plaintext"
    print(f"created {kind} database at {path}")


def cmd_encrypt(args: argparse.Namespace) -> None:
    """Copy a plaintext database into a new SQLCipher-encrypted database."""
    from sqlcipher3 import dbapi2 as sqlcipher

    if not os.path.exists(args.src):
        _die(f"source not found: {args.src}")
    if os.path.exists(args.dst):
        _die(f"destination exists: {args.dst} (refusing to overwrite)")

    con = sqlcipher.connect(args.src)  # opened as plaintext
    try:
        con.execute(f"ATTACH DATABASE '{_esc(args.dst)}' AS enc KEY '{_esc(args.passphrase)}'")
        con.execute("SELECT sqlcipher_export('enc')")
        con.execute("DETACH DATABASE enc")
    finally:
        con.close()
    print(f"encrypted {args.src} -> {args.dst}")


def cmd_decrypt(args: argparse.Namespace) -> None:
    """Copy a SQLCipher-encrypted database into a new plaintext database."""
    from sqlcipher3 import dbapi2 as sqlcipher

    if not os.path.exists(args.src):
        _die(f"source not found: {args.src}")
    if os.path.exists(args.dst):
        _die(f"destination exists: {args.dst} (refusing to overwrite)")

    con = sqlcipher.connect(args.src)
    try:
        con.execute(f"PRAGMA key = '{_esc(args.passphrase)}'")
        con.execute(f"ATTACH DATABASE '{_esc(args.dst)}' AS plain KEY ''")
        con.execute("SELECT sqlcipher_export('plain')")
        con.execute("DETACH DATABASE plain")
    finally:
        con.close()
    print(f"decrypted {args.src} -> {args.dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Family Office database admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a fresh database (schema only)")
    p_create.add_argument("--path", required=True, help="path to the new .db file")
    p_create.add_argument("--passphrase", default="", help="encrypt with this passphrase (omit for plaintext)")
    p_create.add_argument("--seed-demo", action="store_true", help="seed demo data")
    p_create.add_argument("--force", action="store_true", help="overwrite if the file exists")
    p_create.set_defaults(func=cmd_create)

    p_enc = sub.add_parser("encrypt", help="encrypt a plaintext database")
    p_enc.add_argument("--src", required=True)
    p_enc.add_argument("--dst", required=True)
    p_enc.add_argument("--passphrase", required=True)
    p_enc.set_defaults(func=cmd_encrypt)

    p_dec = sub.add_parser("decrypt", help="decrypt an encrypted database")
    p_dec.add_argument("--src", required=True)
    p_dec.add_argument("--dst", required=True)
    p_dec.add_argument("--passphrase", required=True)
    p_dec.set_defaults(func=cmd_decrypt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

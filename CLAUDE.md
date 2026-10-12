# Kelly Family Office: notes for Claude

A local-first FastAPI + SQLite (optionally SQLCipher-encrypted) app that tracks the
Kelly family portfolio. Users run it on their own laptops from a release zip.
Dev docs are in README.md; end-user docs are in USAGE.md and launchers/INSTALL.md; packaging is in DISTRIBUTION.md.

## Setup and run
- Python 3.12 via uv. `uv venv --python 3.12 .venv` then
  `uv pip install --python ./.venv/bin/python --only-binary :all: -e ".[dev]"`
  (`--only-binary` is needed because recent `cryptography` releases try to build from source).
- Run: `./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`
- Tests: `./.venv/bin/python -m pytest`. Tests must stay offline, so mock yfinance/OpenFIGI.
- **Pins that matter:** `fastapi>=0.115,<0.116` / `starlette<0.42`. Newer versions break the
  `TemplateResponse(name, context)` signature, and every page returns a 500.
  `cryptography>=44,<49` (49 has no Intel-mac wheels). `yfinance>=1.5.2`.
- `uv sync --frozen` (what installs use) gives core deps only, with slightly different pins
  than the dev venv. Smoke-test the locked environment before shipping.

## Data and privacy
- Real family data is never committed: `.env`, `*.db`, `data/`, `private/`, `*.csv` are ignored,
  and `scripts/build_release.py` hard-fails if any of them land in a bundle. In a cloud session, use
  seed/test data only.
- The live app runs on the owner's own laptop from its own `data/family_office.db`.
  The copy in Google Drive is an encrypted backup, not what the app reads.

## Codebase rules (each learned from a shipped bug)
- **Never hold a SQLite write transaction across a network call**, and always
  `session.rollback()` after a failed write. A poisoned request session turns a lock wait into a 500.
  Price writes go through short-lived sessions (`market_data._persist_price`); `SnapshotService.capture`
  quotes everything first, then writes in one burst, guarded by `_capture_lock`.
- **Never block a page render on a live network call.** Render paths read stored prices
  (`stored_price_map()`); fetches happen on Refresh, snapshot capture, backfill, or background threads.
- **Every yfinance call** goes through `yahoo_symbol()` (BRK/B→BRK-B, CUSIP→None) and is gated by
  `network_allowed()` or runs in a background worker.
- **Stable value (CASH etc.) is pinned to $1 at the valuation layer** (`get_current_price`
  short-circuits `STABLE_VALUE_SYMBOLS`). Keep that symbol list in sync across market_data, snapshot
  capture and the repair migration.
- **"What do we hold now" must use `app/services/holdings.py`** (`open_lots_query`, `held_asset_ids`),
  never a raw `TaxLot.is_closed == False`, because accounts are soft-deleted via `is_active`. These stay
  deliberately unfiltered: realized YTD/tax reports, disposal lot selection, and the wash-sale window.
- Ruff E712 flags SQLAlchemy's `== False`. That idiom is correct, so don't "fix" it.
- **Imports are snapshot-replace, not append.** Only importer-created rows
  (`Transaction.notes == IMPORT_NOTE`) may be deleted. `imported_lots` / `lot_has_history` /
  `delete_imported_lot` are the single definition of "real history". A row with no `asset_class`
  defaults to US_EQUITY.
- 401k CITs have no public ticker. Keep the CUSIP in `cusip`, set `is_publicly_traded=False`, enter a manual
  price, and optionally a `look_through_ticker` proxy.
- Monte Carlo is **nominal** (no COLA/inflation). SSA, the income bridge and household SSA live in
  `app/services/analysis_service.py`.

## Schema changes
Edit models, then `alembic revision --autogenerate -m "..."`. `alembic/env.py` opens SQLCipher DBs via
`app.services.db.create_db_engine`. Migrations run at startup (backup first).

## Releases (every version)
1. Bump `version` in `pyproject.toml` (`app/version.py` reads it from there).
2. `./.venv/bin/python scripts/build_release.py` → `dist/family-office-<ver>.zip`. Pure Python, so it
   runs on any OS. **Zips are FLAT** (no wrapper directory); the updater depends on that.
3. Tag `vX.Y.Z`, push, then
   `gh release create vX.Y.Z -R getmalone/Family-Office dist/family-office-X.Y.Z.zip`.
   **A release without the zip asset is invisible to zip installs' self-updater.**
4. Dress rehearsal: `git archive <prev tag> | tar -x -C <scratch>`, add sentinel `data/family_office.db`
   and `.env`, patch `updater._ROOT` + `updater.get_app_version`, call `apply_manual_update()`, and confirm the
   swap lands and data/.env survive.
- An updater fix can't deliver itself. A stuck install must take the new version by hand.
- The `/settings/check-updates` endpoint must stay network-only in the service worker.

## Wiki
Canonical source: `app/templates/wiki/index.html` (raw HTML, not Jinja), sections `s01..s19`. It's
published through the `familyoffice-site` repo plus the `crestline-site` aggregator to Cloudflare Pages
(`crestlinepartnersinc.com/wiki/FamilyOffice/`). The Cloudflare deploy needs a wrangler login, so it
won't run from a cloud session.

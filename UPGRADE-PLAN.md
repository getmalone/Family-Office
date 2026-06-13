# App Upgrade Plan

How the Family Office desktop app upgrades itself. **Decisions (locked):**
git-based self-update · prompt on launch · adopt Alembic for migrations.

## Goals
- One-click/low-friction updates for a non-technical user, cross-platform.
- Never lose data; never break on a half-applied schema change.
- No re-download and no repeated macOS Gatekeeper prompts after first install.
- Keep the source repo private.

## Target architecture
**Install = a git checkout** of `getmalone/Family-Office` (not a copied zip).

- **First-time auth:** one-time `gh auth login` (device-code flow — paste a code
  in the browser). gh's git credential helper then handles all future pulls of
  the private repo. (Fallback: a fine-grained read-only PAT.)
- **On every launch, the launcher (`launchers/_run.py` + shell stubs):**
  1. `git fetch --tags` (network failure = non-fatal → run current version offline).
  2. Compare the checked-out release tag to the latest `vX.Y.Z` tag.
  3. If behind → **prompt**: "Update to vX.Y.Z? [Y/n]". If yes:
     a. **Auto-backup** the encrypted DB (`app_settings.backup_database()`).
     b. `git checkout <latest tag>`.
     c. `uv sync --frozen` (apply dependency changes).
  4. Start the server → startup runs **Alembic `upgrade head`** + idempotent
     seeding (already wired).
- **`data/` is git-ignored**, so updates never touch the database or backups.
- **Rollback:** `git checkout <previous tag>` (+ `uv sync`).
- Update mode configurable (`prompt` default | `auto` | `manual`) via a setting.

## Migrations — Alembic
- Wire `alembic/env.py` to use **`app.services.db.create_db_engine`** so it opens
  the SQLCipher-encrypted DB with `PRAGMA key` (a plain engine from the URL would
  fail on encrypted databases).
- Author a **baseline migration** matching the current live schema (incl. the
  columns added ad-hoc: `investment_profiles.is_comparison_a/b`,
  `assets.look_through_ticker`, the `app_settings` table, SSA fields are schema-less).
- **Existing databases:** `alembic stamp head` at the baseline so they aren't
  recreated; future migrations apply incrementally.
- On startup, run `alembic.command.upgrade(cfg, "head")` and **retire the ad-hoc
  `_apply_migrations()`**. Take an auto-backup before applying if the revision will change.
- Schema version lives in Alembic's `alembic_version`; app version stored in
  `AppSetting("app_version")` to detect app upgrades.

## In-app version & update signal
- Show the current version (from package metadata / a `VERSION`) in Settings/Help.
- "Check for updates" button + a banner ("vX.Y.Z available — relaunch to update"),
  since the update itself happens via the launcher at start. Optionally an
  "Update & restart" action that re-execs the launcher.

## Transition from today's zip installs
- **New users:** a tiny bootstrap launcher runs `gh auth login` (if needed) and
  `git clone` into the app folder, then proceeds normally.
- **Existing zip install (e.g. current one):** clone fresh to a new folder and
  copy the old `data/` across (one-time), then use the git-based launcher.

## Implementation phases
**Phase 1 — safety & visibility (no architecture change)**
- [ ] `VERSION` stamp + show it in the UI; store `app_version` in `AppSetting`.
- [ ] `app_settings.backup_database()` hook callable before upgrades/migrations.
- [ ] `scripts/update.sh` (git fetch → checkout latest tag → `uv sync`) for manual use.

**Phase 2 — git self-update (the core)**
- [ ] First-run bootstrap: `gh auth login` (device flow) + `git clone`.
- [ ] Launcher update step (fetch → compare → prompt → backup → checkout → sync).
- [ ] Update-mode setting (prompt/auto/manual); offline-safe; in-app version/update UI.

**Phase 3 — Alembic migrations**
- [ ] `alembic init` + `env.py` using `create_db_engine` (SQLCipher-aware).
- [ ] Baseline migration; `stamp head` for existing DBs.
- [ ] Run `upgrade head` on startup; remove `_apply_migrations()`; auto-backup pre-migrate.

## Risks / edge cases
- Private-repo auth per machine (one-time `gh auth login`).
- Alembic must use the keyed engine for encrypted DBs; baseline must match reality.
- Offline launch must skip the update check gracefully.
- Checkpoint WAL before backup; ensure DB not mid-write.
- First-install bootstrap `.command`/`.bat` is still quarantined once on macOS
  (subsequent git updates are not).

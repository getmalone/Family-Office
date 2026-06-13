#!/usr/bin/env bash
#
# Manually update the Family Office app to the latest release (git installs).
# The launcher does this automatically on start; this is for updating by hand.
#
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"

if [ ! -d .git ]; then
  echo "This isn't a git install — update by re-downloading the latest release."
  exit 1
fi

echo "Fetching latest releases…"
git fetch --tags --quiet
latest="$(git tag --list 'v*' --sort=-v:refname | head -1)"
[ -n "$latest" ] || { echo "No release tags found."; exit 1; }
current="$(git describe --tags --abbrev=0 2>/dev/null || echo 'none')"
echo "current: $current   latest: $latest"
if [ "$current" = "$latest" ]; then
  echo "Already up to date."
  exit 0
fi

echo "Backing up your database (best effort)…"
./.venv/bin/python -c "from app.services.app_settings import backup_database; print(backup_database())" 2>/dev/null \
  || echo "  (backup skipped — set KFO_MASTER_PASSWORD to back up an encrypted DB)"

echo "Updating to $latest…"
git checkout --quiet "$latest"
uv sync --frozen
echo "✅ Updated to $latest. Relaunch the app to use it."

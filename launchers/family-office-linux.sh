#!/usr/bin/env bash
#
# Family Office — Linux launcher.
# Run this file (double-click in your file manager if it's marked executable, or
# run `./family-office-linux.sh` from a terminal). It sets everything up the
# first time, asks for your password, and opens the app in your browser.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Works whether this launcher sits at the app root or inside launchers/.
if [ -f "$HERE/pyproject.toml" ]; then ROOT="$HERE"; else ROOT="$(cd "$HERE/.." && pwd)"; fi
cd "$ROOT"

echo "Family Office — starting up..."

# 1. Ensure uv (fast Python installer) is available — installs to ~/.local/bin,
#    no root required.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one-time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Create the virtual environment and install the exact locked dependencies.
echo "Preparing environment (first run may take a few minutes)..."
uv sync --frozen

# 3. Launch the app (prompts for your password, opens the browser).
uv run --frozen python launchers/_run.py

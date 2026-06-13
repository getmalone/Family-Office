#!/usr/bin/env bash
#
# Family Office — macOS launcher.
# Double-click this file in Finder. A Terminal window opens, sets everything up
# the first time, asks for your password, and opens the app in your browser.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Works whether this launcher sits at the app root or inside launchers/.
if [ -f "$HERE/pyproject.toml" ]; then ROOT="$HERE"; else ROOT="$(cd "$HERE/.." && pwd)"; fi
cd "$ROOT"

echo "Family Office — starting up..."

# 1. Ensure uv (fast Python installer) is available — installs to ~/.local/bin,
#    no administrator rights required.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one-time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Create the virtual environment and install the exact locked dependencies.
#    First run downloads them (needs internet); later runs are instant.
echo "Preparing environment (first run may take a few minutes)..."
uv sync --frozen

# 3. Launch the app (prompts for your password, opens the browser).
uv run --frozen python launchers/_run.py

#!/usr/bin/env bash
#
# Serve the Family Office on the local network so it's usable from a phone,
# tablet, or another computer — installable as an offline-capable web app.
#
# Usage:
#   KFO_ACCESS_CODE=your-passphrase ./scripts/serve.sh        # LAN, gated
#   HOST=127.0.0.1 ./scripts/serve.sh                         # local only
#
# WARNING: binding to 0.0.0.0 exposes the app to your whole network. Always set
# KFO_ACCESS_CODE first unless you are on a trusted, isolated machine.

set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-${KFO_PORT:-8000}}"

# Prefer the project virtualenv if present.
if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

# Show the LAN address so it's easy to open from another device.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<this-machine-ip>')"

if [[ "$HOST" == "0.0.0.0" && -z "${KFO_ACCESS_CODE:-}" ]]; then
  echo "⚠️  KFO_ACCESS_CODE is not set — the app will be OPEN to everyone on your network."
  echo "    Re-run with: KFO_ACCESS_CODE=your-passphrase ./scripts/serve.sh"
  echo
fi

echo "Family Office serving on:"
echo "  • This machine:  http://127.0.0.1:${PORT}"
echo "  • On your LAN:   http://${LAN_IP}:${PORT}   (open on phone/tablet → 'Add to Home Screen')"
echo

exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"

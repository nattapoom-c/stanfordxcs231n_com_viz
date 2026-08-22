#!/usr/bin/env bash
# dev.sh — Build the XCS221 assignment HTML page then serve locally
#
# Usage:
#   bash scripts/dev.sh           # student mode: build + serve
#   bash scripts/dev.sh PS1       # CF mode: build PS1 + serve
#   PORT=3000 bash scripts/dev.sh # custom port
#   PORT=3000 ASSIGNMENT=PS2 bash scripts/dev.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=${PORT:-8081}
ASSIGNMENT="${1:-${ASSIGNMENT:-}}"

# Use venv Python if available (project requires Python 3.12)
if [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python3"
else
  PYTHON="python3"
fi

if [ -n "$ASSIGNMENT" ]; then
    echo "==> Building $ASSIGNMENT (CF mode) ..."
    "$PYTHON" "$REPO_ROOT/scripts/flatten_and_convert.py" --assignment "$ASSIGNMENT"
    SITE_DIR="$REPO_ROOT/site/$ASSIGNMENT"
else
    echo "==> Building assignment (student mode) ..."
    "$PYTHON" "$REPO_ROOT/scripts/flatten_and_convert.py"
    SITE_DIR="$REPO_ROOT/site"
fi

echo ""
echo "==> Serving at http://localhost:$PORT  (Ctrl-C to stop)"
cd "$SITE_DIR" && "$PYTHON" -m http.server "$PORT"
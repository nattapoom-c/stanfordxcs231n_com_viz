#!/usr/bin/env bash
# build_site.sh — Build HTML for one XCS assignment (no server)
# Usage:
#   bash scripts/build_site.sh          # student mode (root IS the assignment)
#   bash scripts/build_site.sh PS1      # CF mode: build PS1

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSIGNMENT="${1:-}"

if [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python3"
else
  PYTHON="python3"
fi

if [ -n "$ASSIGNMENT" ]; then
    echo "==> Building $ASSIGNMENT (CF mode) ..."
    "$PYTHON" "$REPO_ROOT/scripts/flatten_and_convert.py" --assignment "$ASSIGNMENT"
    echo "==> Output: $REPO_ROOT/site/$ASSIGNMENT/index.html"
else
    echo "==> Building assignment (student mode) ..."
    "$PYTHON" "$REPO_ROOT/scripts/flatten_and_convert.py"
    echo "==> Output: $REPO_ROOT/site/index.html"
fi
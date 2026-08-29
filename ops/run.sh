#!/usr/bin/env bash
# Launcher used by PM2. Kept as a script (not an inline PM2 command) so that
# you can run exactly what production runs, by hand, when debugging.
set -euo pipefail

# Resolve the repo root from this script's own location, so it does not
# matter what directory PM2 starts us in.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

if [[ ! -d ".venv" ]]; then
  echo "No .venv found in $ROOT. Run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

mkdir -p logs

# exec so PM2 signals (restart, stop) reach Python directly rather than bash.
exec .venv/bin/python -m gads_write.server

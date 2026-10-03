#!/bin/bash
set -euo pipefail
LENS_PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    exec "$candidate" "$LENS_PLUGIN_DIR/scripts/plugin_entry.py" "$@"
  fi
done
# Hooks must never block the user's work when a runtime is unavailable.
if [ "${1:-}" = "capture" ]; then
  printf '{}\n'
  exit 0
fi
printf 'Lens needs Python 3.11 or newer. Install it from https://www.python.org/downloads/ and try again.\n' >&2
exit 1

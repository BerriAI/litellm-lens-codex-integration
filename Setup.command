#!/bin/bash
cd "$(dirname "$0")" || exit 1
PYTHON=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if "$candidate" -c 'import sys; assert sys.version_info >= (3,11)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Lens Codex needs Python 3.11 or newer. Install Python, then open Setup again."
  open https://www.python.org/downloads/macos/
  read -r -p "Press Return to close."
  exit 1
fi
"$PYTHON" scripts/install_plugin.py || read -r -p "Setup did not finish. Press Return to close."

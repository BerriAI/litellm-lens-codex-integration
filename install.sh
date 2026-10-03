#!/bin/bash
# Keep execution inside this function so an interrupted download cannot run a partial installer.
lens_install() (
  set -euo pipefail
  if [ "$(uname -s)" != Darwin ]; then
    printf 'Automatic Lens setup currently supports macOS.\n' >&2
    exit 1
  fi

  lens_python=""
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
      lens_python="$candidate"
      break
    fi
  done
  if [ -z "$lens_python" ]; then
    printf 'Install Python 3.11 or newer from https://www.python.org/downloads/macos/ then run this command again.\n' >&2
    exit 1
  fi

  lens_temp="$(mktemp -d "${TMPDIR:-/tmp}/lens-install.XXXXXX")"
  trap 'rm -rf "$lens_temp"' EXIT
  if ! curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --connect-timeout 15 --max-time 120 \
      https://raw.githubusercontent.com/BerriAI/litellm-lens-codex-integration/main/lens_codex/bootstrap.py \
      --output "$lens_temp/setup.py"; then
    printf 'Could not download Lens setup. Check your connection and run this command again.\n' >&2
    exit 1
  fi
  "$lens_python" "$lens_temp/setup.py"
)
lens_install

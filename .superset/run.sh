#!/usr/bin/env bash
# Launches the adash TUI dashboard from this workspace's virtualenv.
#
# This is the app itself, a Textual terminal UI, so it wants a real terminal
# pane. It is not the HUD daemon: `adash serve` binds a fixed port (8737) and
# would collide across workspaces, so start that by hand when you need it.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/adash ]]; then
  echo "No .venv found. Run .superset/setup.sh first." >&2
  exit 1
fi

exec .venv/bin/adash "$@"

#!/usr/bin/env bash
# Superset workspace setup for agent-dash.
#
# agent-dash is a pure-Python project (setuptools + pyproject.toml, no lockfile,
# no services, no database). All a fresh workspace needs is its own virtualenv
# with the package installed editable plus the dev extras (pytest, cairosvg).
# The venv lives inside the workspace so worktrees never share interpreters.
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then
  # uv resolves and installs in a couple of seconds; setup runs on every
  # workspace creation, so use it when it's on PATH.
  uv venv --quiet .venv
  uv pip install --quiet --python .venv/bin/python -e '.[dev]'
else
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e '.[dev]'
fi

echo "agent-dash ready: .venv created, editable install done. Run tests with .venv/bin/pytest"

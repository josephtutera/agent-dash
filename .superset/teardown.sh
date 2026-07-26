#!/usr/bin/env bash
# Superset workspace teardown for agent-dash.
#
# Setup only ever creates workspace-local files (the .venv and the editable
# install's egg-info), and nothing global: no containers, no daemons, no shared
# ports. Deleting the worktree normally takes all of it with it, so this is just
# the explicit inverse of setup in case the directory outlives the workspace.
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf .venv .pytest_cache build dist ./*.egg-info

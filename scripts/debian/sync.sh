#!/usr/bin/env bash
# Run inside the guest; host Git metadata is intentionally not shared.
set -euo pipefail
umask 022
source_dir=${1:-/Users/sebst/.codex/worktrees/45bb/stelvio}
workspace="$HOME/src/stelvio-linux"
mkdir -p "$workspace"
rsync -a --delete --exclude=.git --exclude=.venv --exclude=build --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.coverage --exclude='*.egg-info' "$source_dir/" "$workspace/"
cd "$workspace"
uv sync --locked --python 3.12
uv sync --project pkg/traforo-acceptance-tool --locked --python 3.12

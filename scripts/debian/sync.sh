#!/usr/bin/env bash
# Run inside the guest; host Git metadata is intentionally not shared.
set -euo pipefail
umask 022
source_dir=${1:-/Users/sebst/.codex/worktrees/45bb/stelvio}
workspace="$HOME/src/stelvio-linux"
mkdir -p "$workspace"
cd "$workspace"
bundle="$HOME/src/traforo-linux-source.bundle"
if [ -f "$bundle" ]; then
    # This metadata is private to the guest, never the Mac worktree's .git file.
    # Refresh only HEAD/index; the rsync below supplies the actual working files,
    # including deliberate uncommitted development edits.
    heads=$(git bundle list-heads "$bundle")
    [ "$(printf '%s\n' "$heads" | wc -l)" -eq 1 ] || {
        echo 'Source bundle must contain one commit reference' >&2; exit 1;
    }
    read -r revision reference <<< "$heads"
    if [ ! -d "$workspace/.git" ]; then git -C "$workspace" init -q; fi
    if ! git -C "$workspace" remote get-url origin >/dev/null 2>&1; then
        git -C "$workspace" remote add origin "$bundle"
    fi
    git -C "$workspace" fetch -q --no-tags "$bundle" "$reference"
    git -C "$workspace" update-ref --no-deref HEAD "$revision"
    git -C "$workspace" read-tree "$revision"
fi
rsync -a --delete --exclude=.git --exclude=.venv --exclude=build --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.coverage --exclude='*.egg-info' "$source_dir/" "$workspace/"
cd "$workspace"
uv sync --locked --python 3.12
uv sync --project pkg/traforo-acceptance-tool --locked --python 3.12

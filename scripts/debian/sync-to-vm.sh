#!/usr/bin/env bash
# Run on macOS. Source edits and commits remain in the Mac worktree.
set -euo pipefail
worktree_root=$(git rev-parse --show-toplevel)
temporary=$(mktemp -d "${TMPDIR:-/tmp}/traforo-source.XXXXXXXX")
trap 'rm -f "$temporary/source.bundle"; rmdir "$temporary"' EXIT
git -C "$worktree_root" bundle create "$temporary/source.bundle" HEAD
guest_home=$(limactl shell debian -- printenv HOME)
limactl copy "$temporary/source.bundle" "debian:$guest_home/src/traforo-linux-source.bundle"
limactl shell debian -- bash "$worktree_root/scripts/debian/sync.sh" "$worktree_root"

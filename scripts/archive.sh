#!/usr/bin/env bash
# Orca archive hook (orca.yaml scripts.archive): runs before a worktree is removed, once per removal
# attempt (so idempotent) and must finish in < 120 s. D1: Langfuse scores come after cutover.
# TODO(Langfuse batch): post outcome=abandoned here when the worktree is removed un-landed.
set -euo pipefail
echo "archive: ${ORCA_WORKTREE_PATH:-$PWD} (no-op until the Langfuse batch)" >&2

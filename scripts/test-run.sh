#!/usr/bin/env bash
#
# test-run.sh — make the repo's scripts runnable, then run pytest, as ONE
# allowlistable process.
#
# A spoke that just wrote a new shell script must make it executable before the
# suite can exec it. Run standalone, `chmod +x new.sh` trips the `ask` permission
# rule; folded into `chmod +x new.sh && pytest …` it defeats the scope-guard
# auto-allow the moment any redirection or newline appears (the auto-allow only
# fires on a provably-benign single shape). Collapsing both steps into this script
# means the model runs ONE allowlistable command — `scripts/test-run.sh [pytest
# args…]` — and the chmod never prompts. Seed the matching allow rule
# `Bash(scripts/test-run.sh:*)`.
#
# The chmod step is DIFF-SAFE. It only:
#   • restores the executable bit on files git already tracks as 100755 (a fresh
#     checkout or a copy that dropped the bit), and
#   • adds the bit to brand-new untracked *.sh (the script you just wrote).
# It never touches a file git tracks as 100644 (a sourced library), so it cannot
# dirty the worktree or pollute a review diff. Outside a git repo it skips the
# chmod step entirely and just runs pytest.
#
# Usage:
#   test-run.sh                 # run the whole suite (pytest's testpaths)
#   test-run.sh tests/unit      # forward any args straight to pytest
#   test-run.sh -k name -x      # filters, flags, node ids — all pass through
#
set -euo pipefail

# ── Diff-safe chmod of the repo's scripts ────────────────────────────────────
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # Tracked-100755 files whose working-tree bit was dropped — restore it (this
  # only re-matches the index, so it adds no change to the diff). `-z` emits each
  # record NUL-terminated and the path verbatim (no space-splitting, no octal
  # quoting of non-ASCII), so a path with spaces or unicode is handled correctly.
  # Each record is "<mode> <object> <stage>\t<path>": the mode is the first
  # field, the path is everything after the tab.
  while IFS= read -r -d '' entry; do
    if [ "${entry%% *}" = "100755" ]; then
      f="${entry#*$'\t'}"
      [ ! -x "$f" ] && chmod +x "$f"
    fi
  done < <(git ls-files -s -z -- '*.sh')

  # Brand-new untracked scripts — make them runnable so the suite can exec them.
  while IFS= read -r -d '' f; do
    [ -n "$f" ] && chmod +x "$f"
  done < <(git ls-files --others --exclude-standard -z -- '*.sh')
fi

# ── Resolve a pytest runner ──────────────────────────────────────────────────
# Same precedence as the hook layer's detect_pytest (shared/hooks/lib/utils.sh):
# project venv first, then a `pytest` on PATH, then `python3`/`python -m pytest`.
# Inlined rather than sourced — utils.sh arms a telemetry hook span on source,
# which a plain dev runner must not emit.
if [ -x ".venv/bin/pytest" ]; then
  RUNNER=(.venv/bin/pytest)
elif command -v pytest >/dev/null 2>&1; then
  RUNNER=(pytest)
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' >/dev/null 2>&1; then
  RUNNER=(python3 -m pytest)
elif command -v python >/dev/null 2>&1 && python -c 'import pytest' >/dev/null 2>&1; then
  RUNNER=(python -m pytest)
else
  echo "test-run: no pytest runner found (.venv/bin/pytest, pytest, or python -m pytest)" >&2
  exit 1
fi

exec "${RUNNER[@]}" "$@"

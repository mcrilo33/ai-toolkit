#!/usr/bin/env bash
# quality-check: PostToolUse(Write|Edit|MultiEdit). After a .py edit, runs LINT_CMD then TYPECHECK_CMD on that file only; a failure exits 2 with the tool output on stderr, which goes back to
# the model. An empty knob is off and a knob whose binary is not installed is skipped. Fail-open: the edit is made, so a crash here must not read as a finding.
set -Euo pipefail
# shellcheck source=../../scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)/lib.sh"
f="$(jq -r '.tool_input.file_path // empty' <<<"$(cat)")"
[[ "$f" == *.py && -f "$f" ]] || exit 0
load_env
rc=0
for knob in "${LINT_CMD:-}" "${TYPECHECK_CMD:-}"; do
  read -ra cmd <<<"$knob"
  [ "${#cmd[@]}" -gt 0 ] && command -v "${cmd[0]}" > /dev/null || continue
  out="$("${cmd[@]}" "$f" 2>&1)" || { printf 'quality-check: %s %s failed:\n%s\n' "$knob" "$f" "$out" >&2; rc=2; }
done
exit "$rc"

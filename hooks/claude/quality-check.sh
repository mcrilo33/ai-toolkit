#!/usr/bin/env bash
# quality-check: PostToolUse(Write|Edit|MultiEdit). Runs on the edited file only: LINT_CMD then TYPECHECK_CMD for *.py, SHELLCHECK_CMD for *.sh or an extension-less sh/bash/dash/ksh shebang
# (never zsh). A failure exits 2 with the tool output on stderr, back to the model. An empty knob is off, a missing binary is skipped. Fail-open: the edit is made, a crash is no finding.
set -Euo pipefail
# shellcheck source=../../scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)/lib.sh"
f="$(jq -r '.tool_input.file_path // empty' <<<"$(cat)")"
[ -f "$f" ] || exit 0
case "$f" in
  *.py) knobs=(LINT_CMD TYPECHECK_CMD) ;;
  *.sh) knobs=(SHELLCHECK_CMD) ;;
  *) IFS= read -r first < "$f"; [[ "${f##*/}" != *.* && "$first" =~ ^'#!'([^\ ]*/)?(env[[:space:]]+(-S[[:space:]]+)?)?(sh|bash|dash|ksh)([[:space:]]|$) ]] && knobs=(SHELLCHECK_CMD) || exit 0 ;;
esac
load_env
rc=0
for name in "${knobs[@]}"; do
  knob="${!name:-}"
  read -ra cmd <<<"$knob"
  [ "${#cmd[@]}" -gt 0 ] && command -v "${cmd[0]}" > /dev/null || continue
  out="$("${cmd[@]}" "$f" 2>&1)" || { printf 'quality-check: %s %s failed:\n%s\n' "$knob" "$f" "$out" >&2; rc=2; }
done
exit "$rc"

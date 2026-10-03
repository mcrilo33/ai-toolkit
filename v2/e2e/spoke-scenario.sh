#!/usr/bin/env bash
# End-to-end acceptance (06 section 7) against a LOCAL scratch repo with a local bare origin: no GitHub.
# WP0 = steps 1-4 with a hand-run worker-start; later WPs replace the hand-run with the coordinator.
# Run it from a dedicated Orca terminal: that terminal is the scenario's coordinator (Run owner), e.g.
#   orca terminal create --worktree active --command "bash v2/e2e/spoke-scenario.sh"
# Exits non-zero on the first failed assert and prints the Orca object ids. Cleans up after itself.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
# Fixed path: Claude's workspace-trust entry is keyed on the git root, so one path means one entry, ever.
S=/private/tmp/aitk-e2e-scratch; rm -rf "$S" "$S.git"; mkdir -p "$S"; repo_id=""; wt=""; ws=""; run=""; disp=""; step=0

say() { printf '== step %s: %s\n' "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s worktree=%s run=%s dispatch=%s\n' "$step" "$*" "$repo_id" "$wt" "$run" "$disp"; exit 1; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
find_wt() { orca_json worktree list --repo "path:$S" | jq -r '.result.worktrees[] | select(.displayName == "wp0-hello") | .path'; }
cleanup() {
  local ws=""
  [ -z "$disp" ] || orca orchestration worker-stop --dispatch "$disp" --json > /dev/null 2>&1 || true
  [ -z "$repo_id" ] || wt="$(find_wt 2> /dev/null || true)"
  [ -z "$wt" ] || orca worktree rm --worktree "path:$wt" --force --run-hooks --json > /dev/null 2>&1 || true
  [ -z "$repo_id" ] || orca project setup-delete --setup "$repo_id" --json > /dev/null 2>&1 || true
  [ -z "$wt" ] || ws="$(dirname "$wt")"
  case "$ws" in */"$(basename "$S")") rm -rf "$ws" ;; esac   # Orca's per-repo workspace dir
  rm -rf "$S" "$S.git"
}
trap cleanup EXIT

step=1; say "preflight: orca ready, install.sh, scratch repo registered"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
bash "$V2/scripts/install.sh" > /dev/null 2>&1 || fail "install.sh failed"
git init -q -b main "$S" && git init -q --bare "$S.git" && git -C "$S" remote add origin "$S.git"
mkdir -p "$S/.ai-toolkit/bin" "$S/.ai-toolkit/scripts" "$S/.claude/hooks"   # the layout sync.sh will produce (06 section 6)
cp "$V2"/scripts/*.sh "$S/.ai-toolkit/scripts/"; cp "$V2/bin/claude-spoke" "$S/.ai-toolkit/bin/"
cp "$V2/settings/ai-toolkit.env" "$S/.ai-toolkit/ai-toolkit.env"
echo '#!/bin/sh' > "$S/.claude/hooks/guard.sh"
# A target's .ai-toolkit/ is untracked (the user's global gitignore lists it), so a new worktree has no
# copy: the hooks run from the main checkout. Orca's runner is a bash script, so $ORCA_ROOT_PATH expands.
# shellcheck disable=SC2016
printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: $ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh\n  archive: $ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh\n' > "$S/orca.yaml"
printf '.claude/\n' > "$S/.gitignore"
git -C "$S" add -A && git -C "$S" commit -q -m "chore: scratch repo" && git -C "$S" push -q -u origin main
repo_id="$(orca_json repo add --path "$S" | jq -r '.result.repo.id')"
[ -n "$repo_id" ] && [ "$repo_id" != null ] || fail "orca repo add"

step=2; say "provisioned (sync.sh arrives in WP4; GitHub issue A needs a real repo, so the spec is inline)"
step=3; say "worktree create --setup run, then launch claude-spoke and hand-run worker-start from $ORCA_TERMINAL_HANDLE"
out="$(orca_json worktree create --repo "path:$S" --name wp0-hello --setup run)" || fail "worktree create: $out"
wt="$(printf '%s' "$out" | jq -r '.result.worktree.path')"
setup_done() { [ -f "$wt/.ai-toolkit/setup-done" ]; }
wait_for 60 setup_done || fail "Orca's setup hook did not provision $wt"
run="$(orca_mutate orchestration run-create --objective "wp0 e2e" --from "$ORCA_TERMINAL_HANDLE" | jq -r '.result.run.id')"
# --- launch path (Q1, docs/v2/wp0-notes.md): the two-step. Once Orca's agentCmdOverrides.claude points at
# bin/claude-spoke, replace this block with `worker-start --agent claude --model .. --effort ..` (one line).
term="$(orca_json terminal create --worktree "path:$wt" --title wp0-hello-agent --command \
  "$S/.ai-toolkit/bin/claude-spoke --model $SPOKE_MODEL --effort ${E2E_EFFORT:-low} --dangerously-skip-permissions" | jq -r '.result.terminal.handle')"
agent_up() {   # claude is up; the first run per scratch root meets Claude's trust dialog (default "No, exit"): Down+Enter
  [ "$(orca_json terminal show --terminal "$term" | jq -r '.result.terminal.agentIdentity // empty')" = claude ] && return 0
  if orca_json terminal read --terminal "$term" | jq -e '.result.terminal.tail | join(" ") | test("No, exit")' > /dev/null; then
    orca terminal send --terminal "$term" --text $'\e[B' --json > /dev/null; sleep 1
    orca terminal send --terminal "$term" --enter --json > /dev/null
  fi
  return 1
}
wait_for 90 agent_up || fail "claude did not start in terminal $term: $(orca_json terminal read --terminal "$term" | jq -rc '.result.terminal.tail[-6:]')"
SEED='Run the Bash command sleep 120, then send worker_done --outcome succeeded with the summary "wp0 e2e ok" using your preamble command. Nothing else.'
out="$(orca orchestration worker-start --run "$run" --from "$ORCA_TERMINAL_HANDLE" --worktree "path:$wt" --terminal "$term" \
  --task-title "wp0 e2e" --spec "$SEED" --timeout-ms 120000 --json)" || rc=$?
# ---
disp="$(printf '%s' "$out" | jq -r '.result.dispatchId // empty')"
[ "${rc:-0}" -eq 0 ] || fail "worker-start $(printf '%s' "$out" | jq -c '{state: .result.state, stage: .result.failedStage}')"

step=4; say "worktree exists, provisioned by setup.sh, agent working"
[ -n "$wt" ] && [ -d "$wt" ] || fail "no worktree named wp0-hello"
[ -d "$wt/.claude/hooks" ] || fail ".claude/hooks missing: setup did not run"
[ -s "$wt/.ai-toolkit/spoke-run-id" ] || fail "spoke-run-id missing"
agent_working() { orca_json worktree ps | jq -e --arg p "$wt" '[.. | objects | select(.path? == $p) | .agents[]? | select(.state == "working")] | length > 0' > /dev/null; }
wait_for 180 agent_working || fail "agent never reported state working"
id="$(cat "$wt/.ai-toolkit/spoke-run-id")"
claude_env() {   # the environment of the claude process running in $wt
  local pid
  for pid in $(pgrep -x claude); do
    [ "$(lsof -a -p "$pid" -d cwd -Fn 2> /dev/null | sed -n 's/^n//p')" = "$wt" ] && { ps eww -p "$pid" | tr ' ' '\n'; return 0; }
  done
  return 1
}
claude_env | grep -q "^OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=$id" || fail "claude runs without the spoke_run_id OTel attribute"
echo "PASS steps 1-4 (spoke_run_id=$id)"

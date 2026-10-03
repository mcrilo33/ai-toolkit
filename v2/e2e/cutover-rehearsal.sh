#!/usr/bin/env bash
# Cutover rehearsal on a THROWAWAY clone of this repo; GitHub is never touched: the clone's origin is a LOCAL bare repo made from this checkout's objects,
# the issue side is a stubbed gh (AI_TOOLKIT_GH) and the gate is local (LOCAL_GATE=1). It runs the coordinator's checklist: v2 branch -> main merged in ->
# cutover.sh -> tests + shellcheck + sync-twice -> --no-ff merge into main -> sync into itself (regenerates CLAUDE.md) -> install.sh -> one real worker
# dispatched, reviewed and landed on the local bare origin. The clone is registered in Orca once and kept (a removed repo leaves a stale card).
# REHEARSAL_REF (default HEAD) = the commit to treat as branch v2. Run from an Orca terminal; needs pytest + xdist (and shellcheck) on PATH.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
C=/private/tmp/aitk-cutover-rehearsal; O="$C.git"; E="$C.e2e"; SRC="$(git -C "$V2" rev-parse --show-toplevel)"; run=""; coord=""; step=0
say() { printf '== [%ss] step %s: %s\n' "$SECONDS" "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  clone=%s origin=%s run=%s coordinator log: %s\n' "$step" "$*" "$C" "$O" "$run" "$(tail -n 5 "$E/coord.log" 2> /dev/null | tr '\n' '|')"; exit 1; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
cleanup() {   # every terminal and worktree this script made; the clone stays registered
  local d w
  for d in $([ -z "$run" ] || orca_json orchestration worker-list --run "$run" 2> /dev/null | jq -r '.result.workers[]?.dispatchId' 2> /dev/null); do
    orca orchestration worker-stop --dispatch "$d" --json > /dev/null 2>&1 || true; orca orchestration worker-release --dispatch "$d" --json > /dev/null 2>&1 || true
  done
  [ -z "$coord" ] || orca terminal close --terminal "$coord" --json > /dev/null 2>&1 || true
  for w in $(orca_json worktree list --repo "path:$C" 2> /dev/null | jq -r '.result.worktrees[]? | select(.isMainWorktree | not) | .path' 2> /dev/null); do
    orca terminal close --worktree "path:$w" --all --json > /dev/null 2>&1 || true; orca worktree rm --worktree "path:$w" --force --run-hooks --json > /dev/null 2>&1 || true
  done
  orca terminal close --worktree "path:$C" --all --json > /dev/null 2>&1 || true
  [ -z "$run" ] || rm -rf "$HOME/.ai-toolkit/coordinator/$run"; rm -rf "$E"
}
trap cleanup EXIT
mkdir -p "$E"

step=1; say "a local bare origin from this checkout's objects, a clone on branch v2 with origin/main merged in"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
rm -rf "$C" "$O"; git clone -q --bare "$SRC" "$O"
git -C "$O" update-ref refs/heads/v2 "$(git -C "$SRC" rev-parse "${REHEARSAL_REF:-HEAD}")"; git -C "$O" symbolic-ref HEAD refs/heads/main
git clone -q "$O" "$C"; [ "$(git -C "$C" remote get-url origin)" = "$O" ] || fail "origin is not the local bare repo"
git -C "$C" checkout -q -b v2 origin/v2
git -C "$C" merge-base --is-ancestor origin/main HEAD || git -C "$C" merge -q --no-edit origin/main || fail "origin/main does not merge into v2 (modify/delete conflicts: merge it on v2 first)"

step=2; say "cutover.sh --dry-run lists, then cutover.sh makes one commit and tags v1-final at origin/main"
before="$(git -C "$C" rev-parse HEAD)"
bash "$C/v2/scripts/cutover.sh" --dry-run "$C" > "$E/dry.txt" || fail "dry run failed"; [ "$(git -C "$C" rev-parse HEAD)" = "$before" ] || fail "the dry run committed"
{ grep -q '^delete scripts$' "$E/dry.txt" && grep -q '^move v2/scripts -> scripts$' "$E/dry.txt"; } || fail "the dry run does not list the deletions and moves"
bash "$C/v2/scripts/cutover.sh" "$C" > "$E/cutover.txt" || fail "cutover.sh failed: $(tail -n 3 "$E/cutover.txt")"
cd "$C"
{ [ "$(git rev-list --count "$before..HEAD")" = 1 ] && [ -z "$(git status --porcelain)" ] && [ "$(git rev-parse v1-final)" = "$(git rev-parse origin/main)" ]; } || fail "not one clean commit, or v1-final is not origin/main"
for p in v2 tests_v2 mcp dashboard shared/hooks scripts/telemetry settings/ai-toolkit.yml; do [ ! -e "$p" ] || fail "$p survived the cutover"; done
for p in scripts/coordinator.sh tests/conftest.py docs/architecture.md .github/workflows/ci.yml .shellcheckrc orca.yaml; do [ -e "$p" ] || fail "$p is missing"; done
[ "$(grep -c . requirements-dev.txt)" = 3 ] || fail "requirements-dev.txt is not trimmed to 3 packages"
cnt() { git ls-files "$@" | grep -v '\.DS_Store' | xargs cat 2> /dev/null | wc -l | tr -d ' '; }
echo "lines after cutover: code=$(cnt 'scripts/*' 'bin/*' 'hooks/*') e2e=$(cnt 'e2e/*') config=$(cnt 'settings/*' 'langfuse/*' orca.yaml '.github/*') policy=$(cnt 'shared/*') tests=$(cnt 'tests/*') docs=$(cnt 'docs/*' README.md)"

step=3; say "tests, shellcheck, sync twice into a temp repo: no drift (the ci.yml steps)"
pytest -n auto tests -q > "$E/pytest.txt" 2>&1 || fail "tests are red after the cutover: $(tail -n 5 "$E/pytest.txt")"; tail -n 1 "$E/pytest.txt"
if command -v shellcheck > /dev/null; then shellcheck scripts/*.sh bin/claude-spoke hooks/claude/*.sh hooks/git/* e2e/*.sh || fail "shellcheck"; else echo "shellcheck not installed: skipped"; fi
t="$(mktemp -d)"; git -C "$t" init -q; snap() { (cd "$t" && find . -path ./.git -prune -o -type f -exec cksum {} + | LC_ALL=C sort); }
bash scripts/sync.sh "$t" > /dev/null; snap > "$E/s1"; bash scripts/sync.sh "$t" > /dev/null; snap > "$E/s2"; cmp -s "$E/s1" "$E/s2" || fail "a second sync drifts"; rm -rf "$t"

step=4; say "merge v2 into main (--no-ff), push to the LOCAL origin; sync.sh into itself regenerates CLAUDE.md; install.sh wires repo-local hooks"
git checkout -q main; git merge -q --no-ff -m "Merge v2: cutover" v2; git push -q origin main
[ -z "$(git ls-files CLAUDE.md)" ] && [ ! -e CLAUDE.md ] || fail "CLAUDE.md is tracked or present before the self-sync"
bash scripts/sync.sh "$C" > /dev/null; bash scripts/sync.sh "$C" > /dev/null
{ [ -s CLAUDE.md ] && ! head -n 1 CLAUDE.md | grep -q '^---'; } || fail "sync did not generate CLAUDE.md"
awk 'NR == 1 && /^---$/ { fm = 1; next } fm && /^---$/ { fm = 0; next } !fm' shared/rules/guidelines.md | cmp -s - CLAUDE.md || fail "CLAUDE.md is not the stripped guidelines"
[ -z "$(git status --porcelain)" ] || fail "the self-sync dirtied the tree: $(git status --porcelain | head -n 3)"
bash scripts/install.sh "$C" > /dev/null 2>&1 || fail "install.sh failed"
[ "$(git config --local core.hooksPath)" = "$C/.ai-toolkit/hooks/git" ] || fail "core.hooksPath is not repo-local"
! git commit -q --allow-empty -m "chore: no (#1)" 2> /dev/null || fail "pre-commit let a commit onto main through"

step=5; say "register in Orca once, dispatch one real worker (stubbed gh, local gate), review, land on the local origin"
repo_id="$(orca_json repo list | jq -r --arg p "$C" '[.result.repos[] | select(.path == $p)][0].id // empty')"
[ -n "$repo_id" ] || orca_json repo add --path "$C" > /dev/null || fail "orca repo add"
jq -n --arg b "$(printf 'Create hello.txt in the repo root containing exactly the word hello, then commit it (Refs #1).\n\nScope: hello.txt\nGate: plan\nModel: %s low' "$SPOKE_MODEL")" '{number: 1, title: "Add hello.txt", body: $b}' > "$E/issue.json"
jq -c '[{number, body, labels: {nodes: []}, blockedBy: {nodes: []}}]' "$E/issue.json" > "$E/nodes.json"
# shellcheck disable=SC2016
printf '#!/bin/sh\necho "$*" >> %s/gh.log\ncase "$1 $2" in "issue view") cat %s/issue.json ;; "api graphql") if grep -q "^issue close" %s/gh.log; then echo "[]"; else cat %s/nodes.json; fi ;; esac\n' "$E" "$E" "$E" "$E" > "$E/gh"
chmod +x "$E/gh"; printf 'AI_TOOLKIT_GH=%s/gh\nCHECK_CMD="test -f hello.txt"\nLOCAL_GATE=1\nANSWER_MODEL=claude-sonnet-5-5\n' "$E" > .ai-toolkit/ai-toolkit.local.env
: > "$E/coord.log"
coord="$(orca_json terminal create --worktree "path:$C" --title coordinator --command \
  "NOTIFY_CMD=true bash $C/scripts/coordinator.sh --answer auto --cap 1 --drain > $E/coord.log 2>&1; echo \$? > $E/coord.rc" | jq -r '.result.terminal.handle // empty')"
[ -n "$coord" ] || fail "terminal create"
wait_for 90 grep -q ' coordinator: run run_' "$E/coord.log" || fail "the coordinator did not start"
run="$(sed -n 's/.* coordinator: run \(run_[0-9a-f]*\).*/\1/p' "$E/coord.log" | head -n 1)"
wait_for 1500 test -s "$E/coord.rc" || fail "the coordinator did not finish within 25 min"
[ "$(cat "$E/coord.rc")" = 0 ] || fail "coordinator exited $(cat "$E/coord.rc")"
git fetch -q origin; sha="$(git -C "$O" rev-parse main)"
[ "$(git -C "$O" show main:hello.txt | head -n 1)" = hello ] || fail "the local origin has no hello.txt on main"
[ "$(git rev-parse HEAD)" = "$sha" ] || fail "the clone's main is not at the origin's main"
grep -q "^issue close 1 -c landed in $sha" "$E/gh.log" || fail "the issue was not closed with the landed sha"
grep -q '^SUMMARY: ' "$E/coord.log" || fail "no review verdict: the code-review agent did not run from the cutover tree"
! git -C "$O" for-each-ref --format='%(refname)' refs/heads | grep -qv -e '/main$' -e '/v2$' -e '/v2-wp' || fail "the spoke branch is still on the origin"
[ "$(orca_json worktree list --repo "path:$C" | jq '[.result.worktrees[] | select(.isMainWorktree | not)] | length')" = 0 ] || fail "a spoke worktree is still there"
echo "PASS cutover rehearsal in ${SECONDS}s (landed $sha on $O; clone $C stays registered in Orca)"

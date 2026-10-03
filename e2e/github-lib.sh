#!/usr/bin/env bash
# Shared by github-scenario.sh and switch-scenario.sh (source me): helpers + preflight of the real-GitHub e2e on the throwaway PUBLIC repo mcrilo33/ai-toolkit-e2e.
# The only GitHub repo these scripts write to: origin is checked. A stable clone ($G) is registered in Orca ONCE (never unregistered), reset per run to the
# `e2e-base-3` tag (a force-push of the e2e main), synced from this checkout (--local-only), hooks installed; then cwd = $G. Run from an Orca terminal.
# shellcheck disable=SC2034  # variables set here are read by the scripts that source this file
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env; : "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
R=mcrilo33/ai-toolkit-e2e; G=/private/tmp/aitk-e2e-gh; E="$G.e2e"; ANS="${E2E_ANSWER:-auto}"; PH=" ${E2E_PHASES:-next happy negative} "
repo_id=""; run=""; coord=""; step=0; A=""; B=""; mine=""; wt=""; sha=""; rm -rf "$E"; mkdir -p "$E"
say() { printf '== [%ss] step %s: %s\n' "$SECONDS" "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s run=%s coordinator=%s issues=%s\n  coordinator log: %s\n' "$step" "$*" "$repo_id" "$run" "$coord" "$mine" "$(tail -n 5 "$E/coord.log" 2> /dev/null | tr '\n' '|')"
  cp "$E/coord.log" "$G.last-coordinator.log" 2> /dev/null && echo "  full log: $G.last-coordinator.log"; exit 1; }
phase() { [[ "$PH" == *" $1 "* ]]; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
ghe() { gh -R "$R" "$@"; }
mkissue() { ghe issue create -t "$1" -b "$2" ${3:+-l "$3"} | sed 's#.*/##'; }   # title body [label] -> number
drop_worktrees() {   # every non-main worktree of the clone, terminals first
  local w
  for w in $([ -z "$repo_id" ] || orca_json worktree list --repo "path:$G" 2> /dev/null | jq -r '.result.worktrees[]? | select(.isMainWorktree | not) | .path' 2> /dev/null); do
    orca terminal close --worktree "path:$w" --all --json > /dev/null 2>&1 || true
    orca worktree rm --worktree "path:$w" --force --run-hooks --json > /dev/null 2>&1 || true
  done
}
cleanup() {   # also on failure: every terminal this script made is closed, the issues it filed are closed; the repo stays registered
  local d i
  for d in $([ -z "$run" ] || orca_json orchestration worker-list --run "$run" 2> /dev/null | jq -r '.result.workers[]?.dispatchId' 2> /dev/null); do
    orca orchestration worker-stop --dispatch "$d" --json > /dev/null 2>&1 || true; orca orchestration worker-release --dispatch "$d" --json > /dev/null 2>&1 || true
  done
  [ -z "$coord" ] || orca terminal close --terminal "$coord" --json > /dev/null 2>&1 || true
  drop_worktrees; [ -z "$repo_id" ] || orca terminal close --worktree "path:$G" --all --json > /dev/null 2>&1 || true
  for i in $mine; do ghe issue close "$i" > /dev/null 2>&1 || true; done
  [ -z "$run" ] || rm -rf "${AITK_STATE_DIR:-$HOME/.ai-toolkit/coordinator}/$run"; rm -rf "$E"
}
trap cleanup EXIT
step=1; say "preflight: orca ready, clone of $R registered once and reset to e2e-base-3, synced, hooks installed"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
[ -d "$G/.git" ] || git clone -q "git@github.com:$R.git" "$G"
[ "$(git -C "$G" remote get-url origin | sed -E 's#.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')" = "$R" ] || fail "origin of $G is not $R: refusing to touch it"
repo_id="$(orca_json repo list | jq -r --arg p "$G" '[.result.repos[] | select(.path == $p)][0].id // empty')"
[ -n "$repo_id" ] || repo_id="$(orca_json repo add --path "$G" | jq -r '.result.repo.id')"
drop_worktrees; orca terminal close --worktree "path:$G" --all --json > /dev/null 2>&1 || true
git -C "$G" fetch -q --prune --tags origin
if ! git -C "$G" rev-parse -q --verify refs/tags/e2e-base-3 > /dev/null; then   # once: the base = README + orca.yaml + .gitignore + pytest.ini + one pytest job + one test
  git -C "$G" checkout -q -f main; git -C "$G" reset -q --hard "$(git -C "$G" rev-list --max-parents=0 origin/main | tail -n 1)"; mkdir -p "$G/tests" "$G/.github/workflows"
  # shellcheck disable=SC2016
  printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"\n  archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"\n' > "$G/orca.yaml"
  printf '.claude/\n.ai-toolkit/\n/CLAUDE.md\n__pycache__/\n.pytest_cache/\n' > "$G/.gitignore"; printf '[pytest]\npythonpath = .\n' > "$G/pytest.ini"; printf 'def test_smoke():\n    assert True\n' > "$G/tests/test_smoke.py"
  printf 'name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: pip install pytest && pytest -q\n' > "$G/.github/workflows/ci.yml"
  git -C "$G" add -A; git -C "$G" add -f orca.yaml; git -C "$G" -c core.hooksPath=/dev/null commit -q -m "chore: e2e base"; git -C "$G" tag e2e-base-3; git -C "$G" push -q -f origin main refs/tags/e2e-base-3
fi
git -C "$G" checkout -q -f main && git -C "$G" reset -q --hard e2e-base-3 && git -C "$G" clean -fdq
for b in $(git -C "$G" for-each-ref --format='%(refname:short)' refs/heads | grep -vx main); do git -C "$G" branch -q -D "$b"; done
for b in $(git -C "$G" ls-remote --heads origin | sed 's#.*refs/heads/##' | grep -vx main); do git -C "$G" push -q origin --delete "$b"; done
git -C "$G" push -q -f origin main
for i in $(ghe issue list --state open --limit 200 --json number --jq '.[].number'); do ghe issue close "$i" > /dev/null; done
for l in priority hold blocked; do ghe label create "$l" > /dev/null 2>&1 || true; done
bash "$V2/scripts/sync.sh" "$G" --local-only > /dev/null || fail "sync.sh failed"
printf 'CHECK_CMD="pytest -q"\nLOCAL_GATE=0\nANSWER_MODEL=%s\n' "${E2E_ANSWER_MODEL:-claude-sonnet-5-5}" > "$G/.ai-toolkit/ai-toolkit.local.env"
bash "$V2/scripts/install.sh" "$G" > /dev/null 2>&1 || fail "install.sh failed"
cd "$G"

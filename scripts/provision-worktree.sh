#!/usr/bin/env bash
#
# provision-worktree.sh — provision the policy layer of ONE worktree: the git exclude
# entries, the testmon pre-warm, .ai-toolkit/{spoke-run-id,lane,mode,task.md,
# ledger-skeleton.md}, the gitignored .claude/ tree and the spoke's settings.local.json
# allow/deny rules. Shared by worktree-new.sh / worktree-quick.sh and Orca's setup hook, so a
# worktree is gated the same way whoever created it (issues #359, #363).
#
# Usage:
#   provision-worktree.sh [--worktree <dir>] [--repo-root <dir>] [--issue <n|slug>]
#                         [--lane spoke|express|quick] [--mode attended|afk] [--branch <b>]
#                         [--spoke-run-id <id>] [--otel-body-dir <dir>] [--repo-name <name>]
#                         [--orca-worktree-id <id>] [--orca-dispatch-id <id>] [--run-id <id>]
#                         [--identity-only]
#
# Inputs, in precedence order: the flag, then the Orca setup env (ORCA_WORKTREE_PATH,
# ORCA_ROOT_PATH, ORCA_WORKSPACE_NAME), then a derivation (cwd, the git common dir, the
# branch). With only ORCA_* set and cwd = the worktree it needs no flags at all; with flags
# and no Orca it runs standalone.
#
# --identity-only rewrites just .ai-toolkit/{spoke-run-id,lane,mode,identity}: the dispatcher
# runs it once more after worker-start, when the dispatch id first exists. Ids no flag names
# are kept from the previous record, so a re-run never blanks them.
#
# Env: PROVISION_TASK_TITLE / PROVISION_TASK_BODY hand over an already-fetched issue so a
#      caller that fetched them (worktree-new.sh) does not pay a second `gh` round-trip.
#      PROVISION_ORCA_TIMEOUT bounds the Orca skills probe (seconds, default 20).
#
# Idempotent: a second run over a provisioned worktree changes nothing. A genuine
# provisioning failure exits non-zero (Orca's wait-for-setup must never start an agent on a
# broken tree); the best-effort steps (test venv, .testmondata pre-warm, gh fetch) stay
# best-effort.
set -Eeuo pipefail

WT_PROG="provision-worktree"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# set -e alone dies silently; name the failure so a fail-loud exit is also a legible one.
# Main shell only: set -E also fires the trap inside $(...) where a failure is tolerated.
trap '[ "$BASH_SUBSHELL" -eq 0 ] && wt_warn "provisioning FAILED (line $LINENO) — the worktree is NOT fully gated"' ERR

WT_DIR="${ORCA_WORKTREE_PATH:-}"
REPO_ROOT="${ORCA_ROOT_PATH:-}"
ISSUE=""
LANE=""
MODE=""
BRANCH=""
SPOKE_RUN_ID=""
ORCA_WT_ID=""
ORCA_DISPATCH=""
RUN_ID=""
IDENTITY_ONLY=0
OTEL_BODY_DIR=""
REPO_NAME=""
REPO_NAME_SET=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --worktree|--repo-root|--issue|--lane|--mode|--branch|--spoke-run-id|--otel-body-dir|--repo-name|--orca-worktree-id|--orca-dispatch-id|--run-id)
      [ "$#" -ge 2 ] || wt_die "$1 needs a value" ;;
  esac
  case "$1" in
    --worktree)     WT_DIR="$2"; shift 2 ;;
    --repo-root)    REPO_ROOT="$2"; shift 2 ;;
    --issue)        ISSUE="$2"; shift 2 ;;
    --lane)         LANE="$2"; shift 2 ;;
    --mode)         MODE="$2"; shift 2 ;;
    --branch)       BRANCH="$2"; shift 2 ;;
    --spoke-run-id) SPOKE_RUN_ID="$2"; shift 2 ;;
    --otel-body-dir) OTEL_BODY_DIR="$2"; shift 2 ;;
    --repo-name)    REPO_NAME="$2"; REPO_NAME_SET=1; shift 2 ;;
    --orca-worktree-id) ORCA_WT_ID="$2"; shift 2 ;;
    --orca-dispatch-id) ORCA_DISPATCH="$2"; shift 2 ;;
    --run-id)       RUN_ID="$2"; shift 2 ;;
    --identity-only) IDENTITY_ONLY=1; shift ;;
    *)              wt_die "unknown argument: $1" ;;
  esac
done

[ -n "$WT_DIR" ] || WT_DIR="$PWD"
[ -d "$WT_DIR" ] || wt_die "worktree directory not found: $WT_DIR"
WT_DIR="$(cd "$WT_DIR" && pwd)"
git -C "$WT_DIR" rev-parse --git-dir >/dev/null 2>&1 || wt_die "not a git worktree: $WT_DIR"
if [ -z "$REPO_ROOT" ]; then
  _common="$(git -C "$WT_DIR" rev-parse --path-format=absolute --git-common-dir)"
  REPO_ROOT="${_common%/.git}"
  unset _common
fi
[ -d "$REPO_ROOT" ] || wt_die "repo root not found: $REPO_ROOT"

[ -n "$BRANCH" ] || BRANCH="$(git -C "$WT_DIR" branch --show-current 2>/dev/null || true)"
# Issue: flag, else the leading digits of the Orca workspace name, else of the branch leaf.
if [ -z "$ISSUE" ]; then
  _ws="${ORCA_WORKSPACE_NAME:-}"
  _ws="${_ws##*/}"
  [ -n "$_ws" ] || _ws="${BRANCH##*/}"
  case "$_ws" in
    [0-9]*) ISSUE="${_ws%%[!0-9]*}" ;;
    *)      ISSUE="$(wt_slugify "$_ws")" ;;
  esac
  unset _ws
fi
[ -n "$ISSUE" ] || wt_die "could not resolve the issue/slug — pass --issue"

if [ -z "$LANE" ]; then
  if [ -f "$WT_DIR/.ai-toolkit/lane" ]; then LANE="$(cat "$WT_DIR/.ai-toolkit/lane")"
  elif [[ "$ISSUE" =~ ^[0-9]+$ ]]; then LANE="spoke"
  else LANE="express"; fi
fi
if [ -z "$MODE" ]; then
  if [ -f "$WT_DIR/.ai-toolkit/mode" ]; then MODE="$(cat "$WT_DIR/.ai-toolkit/mode")"
  else MODE="attended"; fi
fi
case "$LANE" in spoke|express|quick) ;; *) wt_die "lane must be spoke, express or quick (got '$LANE')" ;; esac
case "$MODE" in attended|afk) ;; *) wt_die "mode must be attended or afk (got '$MODE')" ;; esac

MARKER_DIR="$(wt_marker_script_dir "$WT_DIR")"

# --- git exclude -------------------------------------------------------------
# Make .ai-toolkit/ and .claude/ ignored via the repo's git exclude (resolved
# for this worktree) rather than trusting the consuming repo's committed
# .gitignore: a synced target may ship its own .gitignore without them, and
# then the minted spoke-run-id and the seeded/copied .claude/ runtime config
# (settings.local.json below) would land UNTRACKED and break worktree-done
# (git worktree remove) and worktree-land (untracked-as-dirty guard) — masked
# on dev machines whose personal global git ignore covers .claude/ (#132).
# The exclude entries are local to the repo's git dir, never committed, and
# appended at most once each.
# The `.testmondata*` glob (issue #206) covers the testmon DB the pre-push gate
# writes AND its `-shm`/`-wal` WAL sidecars: unmanaged they read as untracked and
# the #172 ready gate refuses ready/<N> as a dirty tree. The OTel raw-request-body
# dumps live under .ai-toolkit/ (already excluded above), so no extra entry is
# needed for them.
EXCLUDE_FILE="$(git -C "$WT_DIR" rev-parse --git-path info/exclude 2>/dev/null || true)"
if [ -n "$EXCLUDE_FILE" ]; then
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  for entry in '.ai-toolkit/' '.claude/' '.testmondata*'; do
    grep -qxF "$entry" "$EXCLUDE_FILE" 2>/dev/null \
      || printf '%s\n' "$entry" >> "$EXCLUDE_FILE"
  done
fi

if [ "$IDENTITY_ONLY" -eq 0 ]; then
# --- provision the pre-push gate's testmon/xdist deps (issue #342) ------------
# Without a project .venv carrying pytest-testmon/pytest-xdist, this fresh worktree's first
# push runs the FULL suite SINGLE-process (detect_pytest falls back to a bare pytest lacking
# the plugins). Provision a .venv so the first push runs a testmon incremental under -n auto.
# Run the SHIPPED copy from the worktree's marker-script dir (MARKER_DIR is worktree-relative;
# this script runs from REPO_ROOT). Best-effort: ensure-test-venv.sh always exits 0.
if [ -x "$WT_DIR/$MARKER_DIR/ensure-test-venv.sh" ]; then
  bash "$WT_DIR/$MARKER_DIR/ensure-test-venv.sh" "$WT_DIR" || true
fi

# --- pre-warm .testmondata from the hub baseline (issue #276) -----------------
# The first push per fresh worktree otherwise runs the FULL multi-thousand-test suite
# SOLELY to build .testmondata (12-47 min observed): test-select.sh's python tier has no
# incremental path on a worktree whose testmon DB is missing. Copy a maintained baseline
# from the hub's git-common-dir (refreshed by gate-sweep.sh after a green post-land full
# sweep) into the new worktree root, so the first push runs a testmon INCREMENTAL — only
# the branch diff's affected tests — instead of the seed. No path rewrite is needed:
# testmon stores rootdir-relative paths only, so a DB built at one absolute path is fully
# reused at another. Best-effort and guarded: a missing/unreadable baseline copies
# nothing and the first push falls back to today's full-suite seed — NEVER a wrong-green,
# because testmon's own `environment` row (system_packages + python_version) invalidates a
# copied baseline whose venv dep-set differs (a requirements-dev.txt bump re-runs full).
# The `.testmondata*` exclude seeded just above keeps the copied DB from dirtying the tree.
HUB_COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
case "$HUB_COMMON_DIR" in
  "")  ;;                                       # not resolvable — skip the pre-warm
  /*)  ;;                                        # already absolute
  *)   HUB_COMMON_DIR="$REPO_ROOT/$HUB_COMMON_DIR" ;;
esac
if [ -n "$HUB_COMMON_DIR" ] && [ -r "$HUB_COMMON_DIR/.testmondata-baseline" ]; then
  if cp "$HUB_COMMON_DIR/.testmondata-baseline" "$WT_DIR/.testmondata" 2>/dev/null; then
    echo "→ pre-warmed .testmondata (hub baseline; first push runs a testmon incremental)"
  else
    wt_warn "could not copy the .testmondata baseline — first push falls back to the full-suite seed"
  fi
fi
fi

# spoke_run_id: kept when already minted (a re-run must never re-mint), else the caller's
# (worktree-new mints it so its launch + telemetry share one id), else minted here.
mkdir -p "$WT_DIR/.ai-toolkit"
if [ -s "$WT_DIR/.ai-toolkit/spoke-run-id" ]; then
  SPOKE_RUN_ID="$(cat "$WT_DIR/.ai-toolkit/spoke-run-id")"
else
  [ -n "$SPOKE_RUN_ID" ] || SPOKE_RUN_ID="${BRANCH:-$(basename "$WT_DIR")}+$(date +%s)"
  printf '%s\n' "$SPOKE_RUN_ID" > "$WT_DIR/.ai-toolkit/spoke-run-id"
fi
echo "→ spoke_run_id       $SPOKE_RUN_ID"

# Execution mode + lane pointers (#102): stamped alongside spoke-run-id so
# langfuse_spoke_tree.py can tag the reconstructed trace (groupable in Langfuse).
# `lane` is issue-backed → spoke, ad-hoc slug → express; `mode` is `attended` unless a
# supervisor (hub-afk.sh) passed `--mode afk`.
printf '%s\n' "$LANE" > "$WT_DIR/.ai-toolkit/lane"
printf '%s\n' "$MODE" > "$WT_DIR/.ai-toolkit/mode"
echo "→ lane / mode        $LANE / $MODE"

# Identity record (#360): spoke identity is RECORDED here and read back by the guards
# through shared/hooks/lib/identity.sh — the env marker, git-dir pattern and branch slug
# are only its fallbacks. key=value, LF-terminated, no quoting; empty values are allowed
# (empty orca_* means a worktree Orca did not dispatch). Each Orca id is the flag, else (the
# worktree id only) ORCA_WORKTREE_ID, else the previous record. `issue` is the NUMERIC issue only
# (an express lane's slug identity lives in lane/slug). Written to a sibling temp file then
# renamed, so a reader never sees a partial record.
_id_leaf="${BRANCH##*/}"
_id_type=""
case "$BRANCH" in */*) _id_type="${BRANCH%%/*}" ;; esac
_id_issue=""
[[ "$ISSUE" =~ ^[0-9]+$ ]] && _id_issue="$ISSUE"
_id_prev() {
  [ -f "$WT_DIR/.ai-toolkit/identity" ] || return 0
  sed -n "s/^$1=//p" "$WT_DIR/.ai-toolkit/identity" | head -n1
}
_id_orca_wt="${ORCA_WT_ID:-${ORCA_WORKTREE_ID:-$(_id_prev orca_worktree_id)}}"
_id_dispatch="${ORCA_DISPATCH:-$(_id_prev orca_dispatch_id)}"
_id_run="${RUN_ID:-$(_id_prev run_id)}"
# A CR/LF in a value would inject extra record lines (the reader's first match wins).
for _v in "$_id_type" "$_id_leaf" "$SPOKE_RUN_ID" "$_id_orca_wt" "$_id_dispatch" "$_id_run"; do
  case "$_v" in *$'\n'* | *$'\r'*) wt_die "identity value contains a newline: $_v" ;; esac
done
IDENTITY_TMP="$(mktemp "$WT_DIR/.ai-toolkit/identity.XXXXXX")"
if ! {
  printf 'issue=%s\n' "$_id_issue"
  printf 'type=%s\n' "$_id_type"
  printf 'slug=%s\n' "$_id_leaf"
  printf 'mode=%s\n' "$MODE"
  printf 'lane=%s\n' "$LANE"
  printf 'spoke_run_id=%s\n' "$SPOKE_RUN_ID"
  printf 'orca_worktree_id=%s\n' "$_id_orca_wt"
  printf 'orca_dispatch_id=%s\n' "$_id_dispatch"
  printf 'run_id=%s\n' "$_id_run"
} > "$IDENTITY_TMP" || ! chmod 644 "$IDENTITY_TMP" \
  || ! mv -f "$IDENTITY_TMP" "$WT_DIR/.ai-toolkit/identity"; then
  rm -f "$IDENTITY_TMP"
  wt_die "could not write .ai-toolkit/identity"
fi
unset _id_leaf _id_type _id_issue _id_orca_wt _id_dispatch _id_run _v IDENTITY_TMP
echo "→ identity record    .ai-toolkit/identity"
[ "$IDENTITY_ONLY" -eq 0 ] || exit 0

# --- write the task contract to disk (issue #177) ----------------------------
# Anchoring used to be an LLM errand: the seed prompt told the spoke to run
# /source-task, which shells `gh issue view`. The dispatcher already knows the
# issue, so write the contract to <wt>/.ai-toolkit/task.md at spawn and point the
# seed prompts (the default seed prompt and hub-afk's kickoff_for) at it.
# /source-task stays for crash re-anchor (a lost task.md). Numbered issues only —
# an ad-hoc slug has no issue to fetch; best-effort, a gh miss simply leaves no
# task.md and the seed falls back to /source-task. The Scope:/Gate: control lines
# ride along inside the body verbatim.
TASK_MD="$WT_DIR/.ai-toolkit/task.md"
if [[ "$ISSUE" =~ ^[0-9]+$ ]] && [ ! -s "$TASK_MD" ] && command -v gh >/dev/null 2>&1; then
  # Reuse what the caller already fetched (PROVISION_TASK_*) so a spawn makes at most one gh
  # call per field (an unbounded gh here would double the round-trips and, under /afk's
  # synchronous dispatch, add a second hang point). Unset means "not fetched", so `-` (not
  # `:-`) fetches only then and an already-fetched empty value is honoured, not re-fetched.
  TASK_TITLE="${PROVISION_TASK_TITLE-$(gh issue view "$ISSUE" --json title -q .title 2>/dev/null || true)}"
  TASK_BODY="${PROVISION_TASK_BODY-$(gh issue view "$ISSUE" --json body -q .body 2>/dev/null || true)}"
  # Write ONLY a COMPLETE contract — both title AND body present (issue #206). A
  # partial gh failure (e.g. the body fetch dies after the title fetch) that left a
  # title-only task.md would stamp a hollow contract whose mere existence suppresses
  # the seed's /source-task fallback; requiring both means such a fetch leaves no
  # task.md and the fallback still engages (same as a total gh miss). Written
  # ATOMICALLY (temp file + rename) so a mid-write interruption never leaves a
  # partial task.md that the fallback would likewise be fooled by.
  if [ -n "$TASK_TITLE" ] && [ -n "$TASK_BODY" ]; then
    TASK_TMP="$(mktemp "$WT_DIR/.ai-toolkit/task.md.XXXXXX")"
    {
      printf '# Issue #%s: %s\n\n' "$ISSUE" "$TASK_TITLE"
      printf '%s\n' "$TASK_BODY"
    } > "$TASK_TMP"
    mv "$TASK_TMP" "$TASK_MD"
    echo "→ task contract      .ai-toolkit/task.md (#$ISSUE)"

    # --- pre-seed the todo-ledger skeleton (issue #235) ----------------------
    # The step spine is script-stamped, but the ledger LABELS are still the
    # spoke's, so structure is inherited rather than invented: emit one
    # `#<issue>.<slug> · <STEP> — <label>` row (the ledger-schema-guard format)
    # per subtask x the five solo-cycle steps. Subtasks come from a "## Subtasks"
    # section of the body when present, else a single `#<issue>.main` row from the
    # title. Gitignored (.ai-toolkit/), and only written for a COMPLETE contract.
    # The `·`/`—` separators live only in printf FORMAT strings (fixed bytes, never
    # absorbed into a $var), and subtask extraction is awk-based, so nothing here
    # depends on locale-sensitive multibyte matching.
    LEDGER_SKELETON="$WT_DIR/.ai-toolkit/ledger-skeleton.md"
    SUBTASKS=$(printf '%s\n' "$TASK_BODY" | awk '
      /^[[:space:]]*#+[[:space:]]*[Ss]ubtasks/ { grab=1; next }
      grab && /^[[:space:]]*#+[[:space:]]/     { grab=0 }
      grab && /^[[:space:]]*([-*]|[0-9]+[.)])[[:space:]]+/ {
        sub(/^[[:space:]]*([-*]|[0-9]+[.)])[[:space:]]+/, "")
        print
      }')
    SINGLE=""
    if [ -z "$SUBTASKS" ]; then
      SUBTASKS="$TASK_TITLE"
      SINGLE=1
    fi
    {
      printf '# Ledger skeleton for #%s — seed your task ledger from these rows\n\n' "$ISSUE"
      while IFS= read -r label; do
        [ -n "$label" ] || continue
        if [ -n "$SINGLE" ]; then
          slug="main"
        else
          slug=$(printf '%s' "$label" | tr '[:upper:]' '[:lower:]' \
            | tr -cs 'a-z0-9' '-' | sed -e 's/^-//' -e 's/-$//' | cut -c1-24 | sed -e 's/-$//')
          [ -n "$slug" ] || slug="subtask"
        fi
        for step in ANCHOR RED GREEN REVIEW PUSH; do
          printf '#%s.%s · %s — %s\n' "$ISSUE" "$slug" "$step" "$label"
        done
        printf '\n'
      done <<EOF
$SUBTASKS
EOF
    } > "$LEDGER_SKELETON"
    echo "→ ledger skeleton    .ai-toolkit/ledger-skeleton.md"
  fi
fi

# .claude/ is gitignored runtime config (skills, hooks, settings) synced from
# shared/. `git worktree add` checks out only TRACKED files, so without this copy
# the worktree has no active skills/hooks. (`.worktreeinclude` would handle this,
# but it only runs for native `claude -w` worktrees, not `git worktree add`.)
# .review/ and *.bak are excluded — .review/ is per-checkout approval state that
# must start empty, or a push could pass on another worktree's approval.
# A re-run must not clobber the worktree's own settings.local.json (it carries the seeded
# and any user-curated rules), so it is left out of the copy once it exists. A copy failure
# is a REAL provisioning failure: the worktree would carry no hooks, so it dies loudly
# (the ERR trap names the line) instead of handing the agent an ungated tree.
if [ -d "$REPO_ROOT/.claude" ]; then
  echo "→ copying .claude/ runtime config (gitignored; skills + hooks + settings)"
  KEEP_LOCAL=0
  [ -f "$WT_DIR/.claude/settings.local.json" ] && KEEP_LOCAL=1
  if command -v rsync >/dev/null 2>&1; then
    RSYNC_ARGS=(-a --exclude '.review/' --exclude 'worktrees/' --exclude '*.bak')
    [ "$KEEP_LOCAL" -eq 1 ] && RSYNC_ARGS+=(--exclude '/settings.local.json')
    rsync "${RSYNC_ARGS[@]}" "$REPO_ROOT/.claude/" "$WT_DIR/.claude/" \
      || wt_die ".claude/ copy failed (rsync) — refusing to leave an ungated worktree"
  else
    KEPT_LOCAL=""
    if [ "$KEEP_LOCAL" -eq 1 ]; then
      KEPT_LOCAL="$(mktemp)"
      cp "$WT_DIR/.claude/settings.local.json" "$KEPT_LOCAL"
    fi
    COPY_RC=0
    {
      mkdir -p "$WT_DIR/.claude" \
        && cp -R "$REPO_ROOT/.claude/." "$WT_DIR/.claude/" \
        && rm -rf "$WT_DIR/.claude/.review" "$WT_DIR/.claude/worktrees" \
        && find "$WT_DIR/.claude" -name '*.bak' -type f -delete
    } || COPY_RC=$?
    # Restore the worktree's own settings BEFORE a failure exit, so a failed copy never
    # leaves the hub's file in its place.
    if [ -n "$KEPT_LOCAL" ]; then
      mv "$KEPT_LOCAL" "$WT_DIR/.claude/settings.local.json"
    fi
    [ "$COPY_RC" -eq 0 ] || wt_die ".claude/ copy failed (cp) — refusing to leave an ungated worktree"
  fi
fi

# --- seed the spoke's command allowlist ----------------------------------------
# The spoke's PUSH step and marker emission each run as ONE allowlistable
# process — spoke-push.sh and spoke-ready.sh — because Claude Code's Bash matcher
# decomposes a compound command and requires every segment to be allowed, so a
# decorated/chained push (or the intrinsically two-command `ready/N` / `gate/N`
# marker `git tag … && git push …`) never matched a bare exact-push rule and
# always re-prompted (issues #37, #45). Seed those script rules plus a read-only
# helper allowlist (Tier 1 local, Tier 2 network-read); the ship gates
# (push-scope-guard + the pre-push hooks), not permission asks, do the enforcing.
#
# `git branch --show-current` is seeded EXACT — never `git branch:*`, which would
# hand over `git branch -D`. The runner tier (#38) is scoped to the pytest verbs
# and `chmod +x` only. The staging tier (#149) seeds `git add:*` (worktree-confined)
# and the non-destructive `git reset` unstage shapes ONLY — never the broad
# `git reset:*`, which would hand over `git reset --hard` (a working-tree wipe).
# The exec tier (#259) seeds `Bash(./:*)` so a compound self-op's `./<in-tree-script>`
# segment is honored per-segment (see the rule's own comment for the always-on trade-off).
# No directly-destructive verb is seeded: no `python:*` / `python -c:*` / `chmod:*` (bare) /
# `git tag:*` / `git push:*` / `git checkout|clean:*` / `git reset --hard` / `rm` / `mv`.
ALLOW_RULES=(
  "Bash(bash $MARKER_DIR/spoke-push.sh:*)"
  "Bash(bash $MARKER_DIR/spoke-ready.sh:*)"
  # Tier 1 — read-only, no side effects
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)"
  "Bash(git rev-parse:*)" "Bash(git branch --show-current)"
  "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)"
  "Bash(grep:*)" "Bash(rg:*)" "Bash(find:*)" "Bash(echo:*)" "Bash(tree:*)"
  # Tier 2 — network-read / read-only GitHub
  "Bash(git fetch:*)" "Bash(git remote -v)" "Bash(git stash list)"
  "Bash(gh issue view:*)" "Bash(gh pr view:*)"
  # Runner (#38) — the RED→GREEN→test loop + chmod +x on new scripts; the bare
  # python/chmod verbs stay gated (arbitrary exec / unrestricted mode bits).
  "Bash(python -m pytest:*)" "Bash(.venv/bin/python -m pytest:*)" "Bash(pytest:*)"
  "Bash(chmod +x:*)"
  # Staging (#149) — the RED-commit selective stage `git reset -q; git add <file>`
  # runs unattended without a prompt. `git add` is worktree-confined; the reset
  # shapes exclude `--hard`, so no destructive working-tree wipe is ever handed over.
  "Bash(git add:*)"
  "Bash(git reset)" "Bash(git reset -q)"
  "Bash(git reset HEAD:*)" "Bash(git reset -q HEAD:*)"
  # In-worktree self-script execution (#259). Claude Code evaluates a COMPOUND Bash command
  # PER-SEGMENT against permissions.allow (precedence: deny > ask > allow > default-prompt),
  # and a PreToolUse hook's whole-command `allow` does NOT satisfy that per-segment check. So
  # the #253 afk-permission-hook — which classifies the WHOLE command and emits `allow` for a
  # benign scoped self-op — could not suppress the dialog for the #238 smoke
  # `chmod +x X && ./X`: `chmod +x X` matched the rule above but the `./X` segment matched no
  # rule and re-prompted. Seeding `Bash(./:*)` covers that segment deterministically (the
  # in-worktree exec lane classify_permission already APPROVEs, #240 — trusting the spoke to
  # run its own in-tree scripts, no more dangerous than the seeded `pytest`).
  #
  # TRADE-OFF, on purpose: unlike the hook (self-limited to a live /afk drain), a settings
  # rule is ALWAYS-ON — it auto-runs `./…` in ATTENDED sessions in this spoke worktree too,
  # and its coarse prefix cannot express classify's worktree-confinement, so a `./../…`
  # traversal to a PRE-EXISTING out-of-tree script is NOT rejected here. What keeps that
  # residual low-risk is WRITE-CONFINEMENT: the spoke cannot CREATE an out-of-tree
  # `../evil.sh` (a write outside the worktree escalates), so `./../…` can only reach a script
  # that already exists — not an accidental-damage pattern. The deny-scope hooks remain
  # authoritative for directly-typed destructive VERBS (rm/push/chmod — which this rule never
  # grants; spoke-main-guard also catches a traversal to the land script BY NAME), and the
  # pre-push ship gates guard shared state — but none of those inspect a `./`-run script's
  # body, so write-confinement is the load-bearing guard here. An honest cost for no-dialog.
  "Bash(./:*)"
  # Hub-root READ access (#181) — a spoke routinely studies hub scripts/hooks OUTSIDE its
  # own worktree (e.g. reading the hub's .git/hooks/pre-push to understand the push cage),
  # a write-free research read that otherwise fires a permission dialog and, unattended,
  # escalates to blocked/<N>. READ-only and scoped to the hub root subtree — never Edit/Write
  # (a spoke must never mutate the hub). The `Read(//<abs>/**)` form matches Claude Code's
  # absolute-path read pattern (REPO_ROOT is already absolute, so the leading `/` yields `//`).
  "Read(/${REPO_ROOT}/**)"
)

# --- the afk deny-wall (issue #281) -------------------------------------------
# A spoke must ask in PROSE plus a gate marker, never an `AskUserQuestion`. The /afk
# injector cannot answer a QCM: inject_answer sends Escape FIRST (the #74 menu-cancel, built
# for the PLAN-gate QCM), which CANCELS a spoke's AskUserQuestion rather than selecting an
# option, then types free text at the plain prompt behind it. In #271 that left the pane
# reading "User declined to answer questions" with the options intact while the drain logged
# an injected answer -- and the free text it typed answered an EARLIER park, not the question
# actually on screen. Denying the tool removes the QCM the broker cannot drive.
#
# The rule is BARE (no parenthesised specifier) on purpose: a bare tool-name deny removes the
# tool from the model's context entirely, so the spoke never renders a QCM at all, whereas a
# scoped deny would leave the tool present and merely error on use. A deny RULE also outranks
# the permission MODE (rules > mode), so it still fires under the afk `bypassPermissions`
# launch below -- the same precedence that let a global `ask` rule pierce bypass in #238.
#
# afk-ONLY, mirroring the `--permission-mode bypassPermissions` gating below: an attended
# spoke has a human at the keyboard who can answer a QCM, and no injector Esc-cancels it.
# The spoke's own PLAN gate is unaffected in both modes -- it is TAG-based (spoke-ready.sh
# --gate writes gate/<N>, which plan-gate-guard reads), never an AskUserQuestion -- so the
# wall can never strand a spoke at its own gate.
DENY_RULES=()
[ "$MODE" = afk ] && DENY_RULES=("AskUserQuestion")

# wt_warn_deny_rules -> name the deny rules a failed merge did NOT apply. A dropped ALLOW rule
# only costs a permission prompt (loud, and the operator sees it); a dropped DENY rule silently
# removes a safety wall, so an unapplied one must never pass unremarked. No-op when there are
# none — and it guards the COUNT rather than expanding the array, which on an EMPTY array under
# `set -u` aborts on bash 3.2 (the macOS default shell).
wt_warn_deny_rules() {
  local r
  [ "${#DENY_RULES[@]}" -gt 0 ] || return 0
  wt_warn "  the afk deny-wall below is NOT applied — this spoke can still raise a QCM the drain cannot answer:"
  for r in "${DENY_RULES[@]}"; do wt_warn "  deny: $r"; done
}

SETTINGS_LOCAL="$WT_DIR/.claude/settings.local.json"
mkdir -p "$WT_DIR/.claude"
if [ ! -f "$SETTINGS_LOCAL" ]; then
  {
    printf '{\n  "permissions": {\n    "allow": [\n'
    for i in "${!ALLOW_RULES[@]}"; do
      sep=","; [ "$i" -eq "$(( ${#ALLOW_RULES[@]} - 1 ))" ] && sep=""
      printf '      "%s"%s\n' "${ALLOW_RULES[$i]}" "$sep"
    done
    # An attended spoke seeds no deny rules at all, so it keeps no empty "deny" key. Guard on
    # the COUNT and never expand the array bare: `"${DENY_RULES[@]}"` on an EMPTY array is an
    # unbound-variable abort under `set -u` on bash 3.2 (the macOS default shell).
    if [ "${#DENY_RULES[@]}" -gt 0 ]; then
      printf '    ],\n    "deny": [\n'
      for i in "${!DENY_RULES[@]}"; do
        sep=","; [ "$i" -eq "$(( ${#DENY_RULES[@]} - 1 ))" ] && sep=""
        printf '      "%s"%s\n' "${DENY_RULES[$i]}" "$sep"
      done
    fi
    printf '    ]\n  }\n}\n'
  } > "$SETTINGS_LOCAL"
  echo "→ seeded spoke command allowlist (.claude/settings.local.json)"
  if [ "${#DENY_RULES[@]}" -gt 0 ]; then echo "→ seeded afk deny-wall (AskUserQuestion)"; fi
elif command -v jq >/dev/null 2>&1; then
  # Append-without-churn: existing entries keep their order (no jq `unique`,
  # which would lexicographically re-sort a user-curated list); only rules not
  # already present are appended. A malformed file makes jq fail and a
  # zero-byte file yields zero output documents with exit 0 — in both cases
  # warn and leave the file untouched rather than abort the wiring or
  # silently truncate (-s catches the empty-output case).
  RULES_JSON="$(printf '%s\n' "${ALLOW_RULES[@]}" | jq -Rn '[inputs]')"
  # Same empty-array care as the seed path: never expand DENY_RULES bare (bash 3.2 + set -u).
  # An attended spoke merges `[]`, and the jq filter's guard then leaves the file's `deny` key
  # exactly as it found it — adding an empty `deny: []` to a user's settings would be churn.
  if [ "${#DENY_RULES[@]}" -gt 0 ]; then
    DENY_JSON="$(printf '%s\n' "${DENY_RULES[@]}" | jq -Rn '[inputs]')"
  else
    DENY_JSON='[]'
  fi
  TMP_SETTINGS="$(mktemp)"
  # Append-without-churn for BOTH lists: only rules not already present are added, so a
  # pre-existing user deny rule is preserved (dropping one would silently widen the spoke).
  if jq --argjson rules "$RULES_JSON" --argjson deny "$DENY_JSON" \
       '((.permissions.allow // []) as $cur | .permissions.allow = ($cur + ($rules - $cur)))
        | if ($deny | length) > 0
          then ((.permissions.deny // []) as $dcur | .permissions.deny = ($dcur + ($deny - $dcur)))
          else . end' \
       "$SETTINGS_LOCAL" > "$TMP_SETTINGS" 2>/dev/null && [ -s "$TMP_SETTINGS" ]; then
    mv "$TMP_SETTINGS" "$SETTINGS_LOCAL"
    echo "→ merged spoke command allowlist into settings.local.json"
  else
    rm -f "$TMP_SETTINGS"
    wt_warn "could not merge into settings.local.json (invalid JSON?) — add the allow rules yourself:"
    for r in "${ALLOW_RULES[@]}"; do wt_warn "  $r"; done
    wt_warn_deny_rules
  fi
else
  wt_warn "settings.local.json exists but jq is missing — add the allow rules yourself:"
  for r in "${ALLOW_RULES[@]}"; do wt_warn "  $r"; done
  wt_warn_deny_rules
fi

# --- spoke env into settings.local.json (issues #359, #363) ----------------------
# WT_SPOKE (the role marker the land/done guards read, #26) is always written: Claude honours the
# settings `env` block for the tool processes it spawns. The native-OTel pairs ride the same
# block, ADDITIVELY (keys already in the file win, so a re-run is a no-op), but Claude does NOT
# honour those from settings (03-spike.md Round 4 (b)): the dispatcher also passes them as real
# process env on the launch command, and this copy is for tooling that reads the file. Same pair
# set as that prefix (wt_native_otel_env_json), resolved as worktree-new does (env ->
# settings/ai-toolkit.yml -> default-on). Secrets never enter: OTEL_EXPORTER_OTLP_HEADERS /
# Langfuse auth are not in the pair set.
wt_resolve_telemetry_config "${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
if ! command -v jq >/dev/null 2>&1; then
  wt_warn "jq is missing — the spoke env block was not written to settings.local.json"
else
  SPOKE_ENV_JSON="$(jq -cn --arg tag "$ISSUE" '{WT_SPOKE: $tag}')"
  if [ "$AI_TOOLKIT_OTEL" = "1" ]; then
    # The raw-request body dir lives under the gitignored .ai-toolkit/; repo=<name> is the origin
    # basename (never written empty).
    [ -n "$OTEL_BODY_DIR" ] || OTEL_BODY_DIR="$WT_DIR/.ai-toolkit/raw-bodies"
    mkdir -p "$OTEL_BODY_DIR"
    [ "$REPO_NAME_SET" -eq 1 ] || REPO_NAME="$(wt_repo_name "$REPO_ROOT")"
    SPOKE_ENV_JSON="$(wt_native_otel_env_json "$SPOKE_RUN_ID" "$OTEL_BODY_DIR" "$REPO_NAME" \
      | jq -c --argjson spoke "$SPOKE_ENV_JSON" '$spoke + .')"
  fi
  # An unmergeable settings file (malformed or empty JSON) only WARNS and is never truncated,
  # exactly like the allow-rule merge above.
  TMP_SETTINGS="$(mktemp)"
  if jq --argjson spoke_env "$SPOKE_ENV_JSON" '.env = ($spoke_env + (.env // {}))' \
       "$WT_DIR/.claude/settings.local.json" > "$TMP_SETTINGS" 2>/dev/null && [ -s "$TMP_SETTINGS" ]; then
    mv "$TMP_SETTINGS" "$WT_DIR/.claude/settings.local.json"
    echo "→ seeded spoke env (.claude/settings.local.json env)"
  else
    rm -f "$TMP_SETTINGS"
    wt_warn "could not merge the spoke env into settings.local.json (invalid JSON?) — left untouched"
  fi
fi

# --- check the Orca worker skills (issue #359) ----------------------------------
# A worker agent in an Orca worktree needs the `orca-cli` and `orchestration` skills. Warn
# when either is missing — NEVER fail: a missing skill degrades the worker, it does not leave
# the tree ungated, and the Orca CLI can disconnect at ~30s (03-spike.md #12), so a dropped
# probe is a warning too. Silent when the `orca` CLI is absent (a tmux-only host).
# The probe is bounded by PROVISION_ORCA_TIMEOUT seconds (default 20, under the ~30s drop).
# `orca skills installed --json` has no pinned schema, so every string in the output counts
# as a candidate name; a skill is present when one equals its exact name.
check_orca_skills() {
  command -v orca >/dev/null 2>&1 || return 0
  local limit="${PROVISION_ORCA_TIMEOUT:-20}" out pid watchdog rc=0 skill
  out="$(mktemp)"
  orca skills installed --json > "$out" 2>/dev/null &
  pid=$!
  ( sleep "$limit"; kill "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  watchdog=$!
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  if [ "$rc" -eq 143 ] || [ "$rc" -eq 137 ]; then
    wt_warn "orca skills installed timed out after ${limit}s — could not verify the Orca worker skills"
  elif [ "$rc" -ne 0 ]; then
    wt_warn "orca skills installed failed (exit $rc) — could not verify the Orca worker skills"
  elif ! command -v jq >/dev/null 2>&1; then
    wt_warn "jq is missing — could not verify the Orca worker skills"
  elif ! jq -e . "$out" >/dev/null 2>&1; then
    wt_warn "orca skills installed returned unparseable JSON — could not verify the Orca worker skills"
  else
    for skill in orca-cli orchestration; do
      jq -e --arg s "$skill" '[.. | strings] | index($s) != null' "$out" >/dev/null 2>&1 \
        || wt_warn "Orca worker skill '$skill' is not installed — run: orca skills install $skill"
    done
  fi
  rm -f "$out"
  return 0
}
check_orca_skills

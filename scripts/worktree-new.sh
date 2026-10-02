#!/usr/bin/env bash
#
# worktree-new.sh — dispatch ONE task into an Orca-managed worktree with a Claude agent on it.
#
# One task = one issue = one branch = one checkout = its own staging area, hooks, and
# .review/ approval artifacts (the isolation solo-cycle/close-task assume). The sequence
# (docs/orca-migration/03-spike.md, Round 4):
#   1. `orca worktree create --setup skip` (Orca owns the path), then rename the branch to
#      <type>/<n>-<slug> (Orca sanitises `/` in --name);
#   2. provision-worktree.sh, synchronously, with the mode/lane/branch/run-id Orca's own setup
#      hook cannot receive (so an afk spoke never starts as attended);
#   3. `orca terminal create --command "<OTel env> claude ..."` — a launch WE own, because
#      Claude only honours the OTel pairs as real process env (not settings.local.json);
#   4. `orca orchestration worker-start --terminal` delivers the seed prompt behind Orca's
#      worker preamble.
# Prerequisites: Orca >= 1.4.218 with this repo registered, run from an Orca terminal with a
# bound Run (`orca orchestration run-create --objective ...`), and the Orca workspaces dir
# trusted by Claude once (docs/parallel-worktrees.md).
#
# Usage:
#   scripts/worktree-new.sh <issue> [slug] [type] [flags]
#
#   <issue>  GitHub issue number (or a bare slug for ad-hoc work)
#   [slug]   short branch slug; derived from the issue title when omitted (needs gh)
#   [type]   feature | fix | chore   (default: feature)
#
#   -t, --type <t>   branch type (feature|fix|chore) — unambiguous, beats the positional
#                    [type] slot
#   --prompt <text>  seed prompt for the agent (required for an ad-hoc slug; a numbered issue
#                    defaults to "read .ai-toolkit/task.md")
#   --mode <m>       execution mode stamped on the trace (attended|afk; default attended)
#   --subtasks N,M   extra issues this ONE spoke ships on the same branch (#278)
#
# Env: WT_AGENT_MODEL / WT_AGENT_EFFORT pin the agent's model and effort (otherwise
#      spoke-model.env / settings/ai-toolkit.yml decide).
#
# Examples:
#   scripts/worktree-new.sh 42                          # feature/42-<title>
#   scripts/worktree-new.sh 57 null-pointer fix
#   scripts/worktree-new.sh refactor-sync -t chore --prompt "tidy sync"   # chore/refactor-sync
#
set -euo pipefail

WT_PROG="worktree-new"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# Span start clock for the lifecycle/spawn span emitted at the end.
WT_T0="$(wt_now_ms)"

# --- parse flags vs positionals ----------------------------------------------
POSITIONAL=()
PROMPT=""              # seed prompt for the agent
TYPE_FLAG=""           # --type overrides the positional type (no footgun)
MODE="attended"        # execution mode stamped on the trace (attended | afk); #102
# --subtasks N,M: extra issues this ONE spoke ships (#278). NOT named SUBTASKS: that name is
# already taken further down for the ledger skeleton's body-derived subtask list, which would
# silently clobber this one.
SUBTASK_ARG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -t|--type)     [ "$#" -ge 2 ] || wt_die "--type needs a value"; TYPE_FLAG="$2"; shift 2 ;;
    --type=*)      TYPE_FLAG="${1#--type=}"; shift ;;
    --prompt)      [ "$#" -ge 2 ] || wt_die "--prompt needs a value"; PROMPT="$2"; shift 2 ;;
    --prompt=*)    PROMPT="${1#--prompt=}"; shift ;;
    --mode)        [ "$#" -ge 2 ] || wt_die "--mode needs a value"; MODE="$2"; shift 2 ;;
    --mode=*)      MODE="${1#--mode=}"; shift ;;
    # --subtasks N,M (#278): the extra issues batch-plan packed onto THIS spoke. They ride
    # the primary's branch as ordered subtasks instead of each paying a full spoke lifecycle.
    --subtasks)    [ "$#" -ge 2 ] || wt_die "--subtasks needs a value"; SUBTASK_ARG="$2"; shift 2 ;;
    --subtasks=*)  SUBTASK_ARG="${1#--subtasks=}"; shift ;;
    -*)            wt_die "unknown option: $1" ;;
    *)             POSITIONAL+=("$1"); shift ;;
  esac
done

[ "${#POSITIONAL[@]}" -ge 1 ] || wt_die "usage: worktree-new.sh <issue> [slug] [type] [flags]"
ISSUE="${POSITIONAL[0]}"
SLUG_ARG="${POSITIONAL[1]:-}"
TYPE="${TYPE_FLAG:-${POSITIONAL[2]:-feature}}"   # --type wins, else 3rd positional, else feature

case "$TYPE" in
  feature|fix|chore) ;;
  *) wt_die "type must be one of: feature, fix, chore (got '$TYPE')" ;;
esac

# --- locate the main checkout ------------------------------------------------
# Resolve the MAIN worktree root, so creating from inside an existing worktree
# still places siblings next to the real checkout (not next to a worktree).
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
cd "$REPO_ROOT"

# --- preflight: Orca, a coordinator terminal, a bound Run ----------------------
# worker-start needs a live Orca coordinator terminal with a Run bound to it (spike (c)); from
# any other shell it fails consumer_fenced. Fail loud here, before anything is created.
orca_require_version || exit 1
[ -n "${ORCA_TERMINAL_HANDLE:-}" ] \
  || wt_die "dispatch must run from an Orca terminal (ORCA_TERMINAL_HANDLE is unset): worker-start needs a live coordinator terminal"
RUN_ID="$(orca_run_id)" \
  || wt_die "no Orca Run is bound to this terminal; run: orca orchestration run-create --objective '<what this session coordinates>'"
if [[ ! "$ISSUE" =~ ^[0-9]+$ ]] && [ -z "$PROMPT" ]; then
  wt_die "an ad-hoc dispatch needs --prompt (there is no task contract to seed the agent from)"
fi


# --- derive slug + branch ----------------------------------------------------
# An explicitly-passed slug is still slugified, so spaces or odd characters can
# never produce an invalid git ref.
if [ -n "$SLUG_ARG" ]; then
  SLUG="$(wt_slugify "$SLUG_ARG")"
elif [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  if command -v gh >/dev/null 2>&1; then
    TITLE="$(gh issue view "$ISSUE" --json title -q .title 2>/dev/null || true)"
    if [ -n "$TITLE" ]; then
      SLUG="$(wt_slugify "$TITLE")"
    else
      wt_warn "could not fetch issue #$ISSUE title (gh failed, not authed, or no such issue);"
      wt_warn "falling back to a slug from the number — pass an explicit slug to override."
      SLUG="$(wt_slugify "$ISSUE")"
    fi
  else
    wt_warn "gh not found; cannot fetch issue #$ISSUE title — using the number as the slug."
    SLUG="$(wt_slugify "$ISSUE")"
  fi
else
  SLUG="$(wt_slugify "$ISSUE")"
fi
[ -n "$SLUG" ] || wt_die "could not derive a branch slug; pass one explicitly"

# Branch: feature/<id>-<slug> for numeric issues, <type>/<slug> for ad-hoc.
# (This convention is what source-task / solo-cycle / commit-quality expect —
# which is why we keep the script instead of native `claude -w`'s worktree-<name>.)
if [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  BRANCH="${TYPE}/${ISSUE}-${SLUG}"
  WT_TAG="$ISSUE"
  LANE="spoke"            # issue-backed full solo-cycle (#102)
else
  BRANCH="${TYPE}/${SLUG}"
  WT_TAG="$SLUG"
  LANE="express"          # ad-hoc, no-issue express spoke (#102)
fi

# --- packed subtasks (issue #278) --------------------------------------------
# batch-plan emits an ordered GROUP per spoke ("263,265 270"): same-scope issues that could
# never run concurrently ride ONE branch as ordered subtasks, instead of each paying the
# whole spoke lifecycle tax (worktree, first-push suite seed 12-47 min, PLAN gate, review,
# land). The branch keeps leading with the PRIMARY — inflight_worktrees and worktree-land
# both parse the issue out of the leading digits of the branch slug — and the extra issues
# go into the queued-subtask channel, which is what holds the spoke's terminal
# ready/<primary> until it has shipped them all.
#
# SUBTASK_LIST is normalized here (validated, primary dropped, deduped) so the seed prompt
# below and the queue seeding agree on one list.
SUBTASK_LIST=()
if [ -n "$SUBTASK_ARG" ]; then
  [[ "$ISSUE" =~ ^[0-9]+$ ]] || wt_die "--subtasks needs a numbered primary issue (got '$ISSUE')"
  _seen=" "
  # noglob for the split: the word-splitting expansion below is deliberately unquoted, so
  # without this `--subtasks '2*'` GLOBS against the cwd and any files named 20/21/22 expand
  # into the list — and then sail through the numeric check below as if they were real issue
  # numbers. Validation cannot catch that; it runs after the shell has already substituted.
  # (batch-plan.sh disables globbing script-wide for the same class of hazard.) Restored
  # immediately after, since the rest of this script relies on globbing.
  set -f
  # Split on commas AND whitespace, so `--subtasks "265, 270"` works like `265,270`.
  for _st in ${SUBTASK_ARG//,/ }; do
    # Fail LOUDLY rather than skip: a malformed entry silently dropped would lose a real
    # issue from the group, and nothing downstream would notice it was meant to ship here.
    [[ "$_st" =~ ^[0-9]+$ ]] || { set +f; wt_die "--subtasks: '$_st' is not an issue number"; }
    # `continue` on a match, but never let the FAILING test abort the loop under `set -e`:
    # a test followed by && is exempt, and an if/fi keeps that obvious rather than subtle.
    if [ "$_st" != "$ISSUE" ]; then           # the primary IS the branch, never a subtask
      case "$_seen" in
        *" $_st "*) ;;                        # dedupe: already queued
        *) _seen="${_seen}${_st} "; SUBTASK_LIST+=("$_st") ;;
      esac
    fi
  done
  set +f
  unset _seen _st
fi

# --- per-issue Model: override (issue #142) ----------------------------------
# A `Model: <id>` line in a numbered issue's body pins THIS spoke's driver model
# (same first-match, case-insensitive convention as Scope:/Gate:). An explicit
# WT_AGENT_MODEL from the environment always wins, so this only runs when unset.
if [ -z "${WT_AGENT_MODEL:-}" ] && [[ "$ISSUE" =~ ^[0-9]+$ ]] && command -v gh >/dev/null 2>&1; then
  ISSUE_BODY="$(gh issue view "$ISSUE" --json body -q .body 2>/dev/null || true)"
  MODEL_LINE="$(printf '%s\n' "$ISSUE_BODY" \
    | grep -iE '^[[:space:]]*model:[[:space:]]*[^[:space:]]' | head -1 || true)"
  if [ -n "$MODEL_LINE" ]; then
    WT_AGENT_MODEL="$(printf '%s' "$MODEL_LINE" \
      | sed -E 's/^[[:space:]]*[Mm][Oo][Dd][Ee][Ll]:[[:space:]]*//; s/[[:space:]]+$//')"
    echo "→ per-issue model  $WT_AGENT_MODEL (from issue #$ISSUE Model: line)"
  fi
fi

WT_NAME="${BRANCH##*/}"   # Orca sanitises `/` in --name, so the branch is renamed below

# --- create the worktree through Orca ----------------------------------------
git fetch origin --quiet 2>/dev/null || true

if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  wt_die "branch already exists locally: ${BRANCH} — check it out, or pass a different slug"
fi
if git show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
  wt_warn "branch ${BRANCH} already exists on origin; the new worktree starts a fresh local branch."
fi

# Branch from the RESOLVED base (issue #117) — origin/<base> when the remote
# ref exists, else the local <base> — never implicitly from the hub's HEAD, so
# a repo integrating on develop (git config ai-toolkit.base-branch) cuts spokes
# from the right place and hub-local drift is not inherited.
BASE_BRANCH="$(wt_base_branch "$REPO_ROOT")"
BASE_START="$(wt_base_start_point "$REPO_ROOT")" \
  || wt_die "base branch '$BASE_BRANCH' has no ref (neither origin/$BASE_BRANCH nor local) — fix git config ai-toolkit.base-branch / AI_TOOLKIT_BASE_BRANCH"

ISSUE_ARGS=()
[[ "$ISSUE" =~ ^[0-9]+$ ]] && ISSUE_ARGS=(--issue "$ISSUE")
echo "→ creating worktree  $WT_NAME (via Orca, from $BASE_START)"
_wt_probe() { orca_worktree_by_name "$WT_NAME" "path:$REPO_ROOT"; }
orca_call_settled _wt_probe worktree create --repo "path:$REPO_ROOT" --name "$WT_NAME" --setup skip \
  --no-parent --base-branch "$BASE_START" --comment "$BRANCH ($MODE)" ${ISSUE_ARGS[@]+"${ISSUE_ARGS[@]}"} \
  || wt_die "orca worktree create failed: ${ORCA_ERR:-$ORCA_OUT}"
WT_DIR="$(orca_wt_path)"; WT_ID="$(orca_wt_id)"
[ -d "$WT_DIR" ] || wt_die "orca returned no usable worktree path: $ORCA_OUT"
git -C "$WT_DIR" branch -m "$BRANCH" || wt_die "could not rename the Orca branch to $BRANCH in $WT_DIR"
orca_json worktree set --worktree "id:$WT_ID" --workspace-status in-progress \
  || wt_warn "could not mark the Orca worktree in-progress"
echo "→ new branch         $BRANCH"

# Resolve the marker-emitter dir that EXISTS in the freshly-created worktree (#271): `scripts`
# in the ai-toolkit checkout (tracked, so it is checked out), `.ai-toolkit/scripts` in a synced
# target. The seed prompt + allowlist below name this path so the spoke's `bash <dir>/
# spoke-ready.sh …` marker command is runnable verbatim (and matches the deny-wall's tier-1 lane).
MARKER_DIR="$(wt_marker_script_dir "$WT_DIR")"

# --- provision the worktree's policy layer (issue #359) -----------------------
# Everything that makes this checkout a GATED spoke — git excludes, test venv, .ai-toolkit/
# {spoke-run-id,lane,mode,identity,task.md,...}, the copied .claude/ tree and the seeded
# settings.local.json — lives in provision-worktree.sh, the SAME script Orca's setup hook runs.
# The spoke_run_id (<branch>+<spawn-epoch>) is minted here: every hook/script emitting telemetry
# in this worktree reads it, so all spans of one spoke share it across sessions and resumes.
# TITLE / ISSUE_BODY (fetched above, when they were) ride along so the provisioner does not
# pay a second `gh` round-trip per field; unset means "not fetched", so it fetches itself.
SPOKE_RUN_ID="${BRANCH}+$(date +%s)"
PROVISION_ENV=()
[ -n "${TITLE+x}" ] && PROVISION_ENV+=("PROVISION_TASK_TITLE=$TITLE")
[ -n "${ISSUE_BODY+x}" ] && PROVISION_ENV+=("PROVISION_TASK_BODY=$ISSUE_BODY")
_wt_provision() {
  env -u PROVISION_TASK_TITLE -u PROVISION_TASK_BODY ${PROVISION_ENV[@]+"${PROVISION_ENV[@]}"} \
    bash "$SCRIPT_DIR/provision-worktree.sh" \
    --worktree "$WT_DIR" --repo-root "$REPO_ROOT" --issue "$WT_TAG" --lane "$LANE" \
    --mode "$MODE" --branch "$BRANCH" --spoke-run-id "$SPOKE_RUN_ID" \
    --otel-body-dir "$WT_DIR/.ai-toolkit/raw-bodies" --repo-name "$(wt_repo_name "$REPO_ROOT")" \
    --orca-worktree-id "$WT_ID" --run-id "$RUN_ID" "$@"
}
_wt_provision \
  || wt_die "provisioning $WT_DIR failed — the worktree is NOT gated; fix the error above, then re-run: bash $SCRIPT_DIR/provision-worktree.sh --worktree $WT_DIR --repo-root $REPO_ROOT --issue $WT_TAG --lane $LANE --mode $MODE --branch $BRANCH --orca-worktree-id $WT_ID"
SPOKE_RUN_ID="$(cat "$WT_DIR/.ai-toolkit/spoke-run-id")"

# The task contract path (written by provision-worktree.sh) the default seed prompt points at.
TASK_MD="$WT_DIR/.ai-toolkit/task.md"

# Resolve the spoke driver's default model/effort via the shared helper (issue #142,
# #233): sync-emitted spoke-model.env -> hub config -> literal defaults. An explicit
# WT_AGENT_MODEL / WT_AGENT_EFFORT (env, or the Model: line above) always wins.
# The hub-side config path honors AI_TOOLKIT_CONFIG like sync-to-repo.sh does.
WT_CONFIG="${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
wt_resolve_agent_model "$SCRIPT_DIR" "$WT_CONFIG"

# --- native-OTel launch env (issues #83, #126, #228, #343) -------------------
# DEFAULT-ON unless the operator opts out with AI_TOOLKIT_OTEL=0 (a clean, full opt-out; the
# preflights below read the same variable). The pair set is wt_native_otel_env_pairs, the single
# source shared with provision-worktree.sh and spoke-relaunch.sh. Claude only honours these as
# real PROCESS env, so they ride the launch command (spike (b)). The auth header
# (OTEL_EXPORTER_OTLP_HEADERS) is never wired: it stays in the inherited environment, off the
# command line (visible in `ps`/the terminal).
#
# !!! PRIVACY !!!  Default-on means EVERY spoke ships CONTENT off-box: user prompts and per-tool
# input/output (OTEL_LOG_*) and, to .ai-toolkit/raw-bodies (local), each full request body.
# AI_TOOLKIT_OTEL=0 opts out entirely.
wt_resolve_telemetry_config "${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
OTEL_PREFIX=""
if [ "${AI_TOOLKIT_OTEL:-}" = "1" ]; then
  # The span-sink endpoint must ALSO be set in THIS shell: wt_emit_* below run telemetry.sh,
  # whose OTLP sink fires only when it is set (the prefix helper defaults it in a subshell).
  wt_default_span_endpoint
  OTEL_PREFIX="$(wt_native_otel_prefix "$SPOKE_RUN_ID" "$WT_DIR/.ai-toolkit/raw-bodies" "$(wt_repo_name "$REPO_ROOT")")"
fi

# Default seed prompt (issue #177): with no caller-supplied --prompt, seed the
# spoke to READ its on-disk task contract instead of anchoring via an LLM
# /source-task round-trip. An explicit --prompt (start-task, hub-afk's
# kickoff_for) still wins; an ad-hoc slug has no task.md and must pass --prompt.
if [ -z "$PROMPT" ] && [ -f "$TASK_MD" ]; then
  # A packed spoke (#278) owns a CHAIN, not one issue. Without this the spoke reads task.md,
  # sees a single issue, and has no idea the queue is waiting on it — so name the queued
  # issues and the order of work up front. The queue is still the mechanical authority
  # (spoke-ready.sh refuses ready/${ISSUE} while it is non-empty); this is the heads-up that
  PROMPT="Read your task contract at .ai-toolkit/task.md (issue #${ISSUE}, fetched at spawn -- no need to run /source-task). Break it into a task ledger (one entry per subtask x the solo-cycle steps ANCHOR/RED/GREEN/REVIEW/PUSH, exactly one in_progress) -- a skeleton is pre-seeded at .ai-toolkit/ledger-skeleton.md; seed your ledger from its rows so your entries match the '#<issue>.<slug> - <STEP> - <label>' schema. Honor its Gate: line: plan (the default for non-trivial work, and whenever no Gate: line is present) means the PLAN gate comes first -- explore, print the full implementation plan, emit 'bash ${MARKER_DIR}/spoke-ready.sh --gate ${ISSUE}', and WAIT for approval before GREEN; only Gate: none runs autonomous straight through. Then implement via the solo-cycle (/cycle: RED -> GREEN -> REVIEW -> PUSH). Push your own branch each subtask; when the acceptance criteria are all met, push the final subtask and emit 'bash ${MARKER_DIR}/spoke-push.sh --ready ${ISSUE}'. Do NOT self-land. If task.md is missing, or the issue was edited after spawn, run /source-task ${ISSUE} to re-anchor from the live issue."
fi
# A numbered issue whose contract fetch failed (no task.md) still needs a seed: re-anchor from
# the live issue, exactly as the default prompt's own fallback sentence says.
[ -n "$PROMPT" ] || PROMPT="/source-task ${ISSUE}"

# A packed spoke (#278) owns a CHAIN, not one issue: untold, it reads task.md, sees a single
# issue, and has no idea a queue is waiting on it — then hits an unexplained ready/${ISSUE}
# refusal at the very end. So APPEND the chain note to whatever prompt is being seeded,
# INCLUDING a caller-supplied --prompt. That placement is load-bearing: every packed dispatch
# comes from hub-afk's dispatch_batch, which ALWAYS passes --prompt "$(kickoff_for ...)", so a
# note built only on the default-prompt branch above would never once reach a real packed
# spoke. Appending here also keeps --subtasks self-contained — no caller has to duplicate the
# wording for packing to work.
#
# The queue stays the mechanical authority (spoke-ready.sh refuses the terminal ready while it
# is non-empty); this is the heads-up that stops that refusal from being a surprise.
if [ "${#SUBTASK_LIST[@]}" -gt 0 ] && [ -n "$PROMPT" ]; then
  PROMPT="${PROMPT} This spoke also carries these PACKED subtask issues on this SAME branch, in order: ${SUBTASK_LIST[*]} (they share #${ISSUE}'s scope, so ONE spoke ships them all instead of each paying a full spawn + suite-seed + review + land cycle). Finish #${ISSUE}'s own work first, then for EACH queued issue run '/source-task <N>' to re-anchor, run its full solo-cycle, and emit 'bash ${MARKER_DIR}/spoke-push.sh --ready <N>' -- that clears it from the queue. Inspect the queue any time with 'bash ${MARKER_DIR}/spoke-ready.sh --queued ${ISSUE}'. Only once the queue is EMPTY do you emit the terminal 'bash ${MARKER_DIR}/spoke-push.sh --ready ${ISSUE}' -- it is REFUSED while the queue is non-empty."
fi

# afk_ask_rule_preflight -> warn (stderr, best-effort) when the user-global settings carry a
# `permissions.ask` rule that would PIERCE bypassPermissions (rules > mode, #238) and strand this
# afk spoke on a dialog. Advisory only: never blocks the spawn, and is a silent no-op when the
# settings file / python3 is absent or no ask rule exists. HONORS CLAUDE_CONFIG_DIR (the CC
# config root) before falling back to ~/.claude, matching where CC reads user settings.
afk_ask_rule_preflight() {
  local settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" n
  [ -f "$settings" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  n="$(WT_ASK_SETTINGS="$settings" python3 - <<'PYEOF' 2>/dev/null || printf 0
import json, os
try:
    with open(os.environ["WT_ASK_SETTINGS"]) as fh:
        rules = ((json.load(fh) or {}).get("permissions") or {}).get("ask") or []
    print(len(rules) if isinstance(rules, list) else 0)
except Exception:
    print(0)
PYEOF
)"
  case "$n" in '' | 0 | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "worktree-new: WARNING (afk): $settings has $n permissions.ask rule(s) -- an ask RULE pierces bypassPermissions (rules > mode) and can strand this spoke on a dialog. Remove global ask rules (or use an afk-aware machine-local hook) for a dialog-free drain." >&2
}

[ "$MODE" = afk ] && afk_ask_rule_preflight
# WT_SPOKE marks the session's ROLE, not its directory (issue #26): every command the spoke runs
# inherits it, so the land and done scripts refuse a spoke that cd's to the hub and tries to land
# or tear down its own worktree. --dangerously-skip-permissions mirrors Orca's own new-agent-tab
# default (Q2: no extra permission mechanism; the hooks stay the cage).
AGENT_CMD="${OTEL_PREFIX}WT_SPOKE=$(printf '%q' "$WT_TAG") claude --model $(printf '%q' "$WT_AGENT_MODEL") --effort $(printf '%q' "$WT_AGENT_EFFORT") --dangerously-skip-permissions"

# Bring up the otelcol collector, then the Langfuse message bridge, before the
# spoke starts streaming, so an opted-in (AI_TOOLKIT_OTEL=1) spoke auto-populates
# Langfuse with no manual step. Order matters: the collector (:4317, what CC
# exports to) forks to the bridge (:4319), so it must be up first. Both are
# idempotent (never a second instance) and best-effort (warn, never fail the spawn).
wt_otel_collector_preflight "$REPO_ROOT"
wt_otel_bridge_preflight "$REPO_ROOT"

# --- launch: a terminal we own, then the seed through worker-start ------------
# The agent must be RUNNING before worker-start types the prompt, or the text would reach a bare
# shell (spike (f)). A claude parked on its workspace-trust dialog defaults to "No, exit", so a
# blocked launch fails loud and is never auto-answered.
_wt_die_if_trust_blocked() {
  [ "$(orca_blocked_reason)" = agent-trust-workspace ] || return 0
  wt_die "Claude is blocked on its workspace-trust dialog (terminal ${TERM_H:-?}). One-time fix: set projects[\"$(dirname "$WT_DIR")\"].hasTrustDialogAccepted to true in ~/.claude.json (docs/orca-migration/03-spike.md Q1); nothing was auto-answered"
}
orca_json terminal create --worktree "path:$WT_DIR" --title "$WT_NAME" --command "$AGENT_CMD" \
  || wt_die "orca terminal create failed: ${ORCA_ERR:-$ORCA_OUT} (worktree kept at $WT_DIR)"
TERM_H="$(orca_terminal_handle)"
orca_wait_agent "$TERM_H" || wt_die "claude did not start in Orca terminal $TERM_H (worktree kept at $WT_DIR)"
orca_json terminal wait --terminal "$TERM_H" --for tui-idle --timeout-ms 20000 || true
_wt_die_if_trust_blocked
# Re-check: the shell prompt satisfies tui-idle too, so claude may have exited during that wait.
orca_wait_agent "$TERM_H" || wt_die "claude is no longer running in Orca terminal $TERM_H (worktree kept at $WT_DIR)"
orca_call_settled "" orchestration worker-start --run "$RUN_ID" --terminal "$TERM_H" \
  --worktree "path:$WT_DIR" --task-title "$WT_NAME" --spec "$PROMPT" \
  || { _wt_die_if_trust_blocked; wt_die "orca worker-start failed (never re-issued): ${ORCA_ERR:-$ORCA_OUT} — terminal $TERM_H, worktree $WT_DIR"; }
DISPATCH_ID="$(orca_dispatch_id)"
echo "→ launched claude in Orca terminal $TERM_H (dispatch ${DISPATCH_ID:-?}, run $RUN_ID)"

# #300 writer: record `dispatched` — the actor that CAUSES the transition (this
# spawn) records it once the launch has succeeded. Shadow-only: the drain still reads
# dispatch-<issue>.epoch and nothing decides on the log yet. AFK_TLOG_RUN stamps
# the freshly-minted spoke_run_id onto the record, so a spoke's whole lifecycle is
# greppable by run even across a relaunch. Best-effort (wt_tlog_* no-op without the
# lib, and skip an ad-hoc slug with no issue number).
AFK_TLOG_RUN="$SPOKE_RUN_ID" wt_tlog_transition "$ISSUE" dispatched worktree-new.sh \
  "spawn --mode $MODE" "{\"branch\":\"$BRANCH\",\"lane\":\"$LANE\",\"mode\":\"$MODE\"}"

# --- seed the queued-subtask channel (issue #278) ----------------------------
# The packed group's extra issues become this spoke's subtask queue. Seeded HERE, at spawn,
# rather than only on hub-afk's routing pass: /next-batch dispatches interactively with no
# drain running, so a packed spoke would otherwise find an empty queue, emit ready/<primary>,
# and silently drop its subtasks on the floor.
#
# Placed AFTER the launch has succeeded on purpose: this dir is keyed by ISSUE and SHARED, not
# worktree-local, so a spawn that dies after seeding would strand it — a later, unrelated spoke
# for the same issue would inherit the entries and be refused at ready/<primary> forever. The
# launch is the last step that can fail; the spoke only reads the queue at ready time.
#
# The path contract (<git-common-dir>/ai-toolkit-afk/queued-<spoke>/<issue>, one empty file
# per queued issue) is INLINED rather than sourced: its owner, gate-broker-markers.sh, is a
# hub-skill module this script cannot reach from a synced target — the same split the
# outbound event spool already lives with, where the two sides share only the path. One file
# per issue keeps create/unlink atomic, so this can never race the spoke's own clears.
# Best-effort: the queue is a scheduling optimization and must not fail an otherwise-fine
# spawn (the spoke would simply ship its primary and the subtasks dispatch fresh).
if [ "${#SUBTASK_LIST[@]}" -gt 0 ]; then
  # --git-common-dir answers RELATIVE to cwd (a bare `.git` from the checkout root), so
  # anchor it on $REPO_ROOT rather than trusting wherever this script happens to stand.
  _q_common="$(cd "$REPO_ROOT" && git rev-parse --git-common-dir 2>/dev/null || printf '.git')"
  case "$_q_common" in /*) ;; *) _q_common="$REPO_ROOT/$_q_common" ;; esac
  _q_dir="${AFK_STATE_DIR:-$_q_common/ai-toolkit-afk}/queued-$ISSUE"
  mkdir -p "$_q_dir" 2>/dev/null || true
  for _st in "${SUBTASK_LIST[@]}"; do
    : > "$_q_dir/$_st" 2>/dev/null || true
  done
  echo "→ queued subtasks    ${SUBTASK_LIST[*]} (shipped on this branch before ready/$ISSUE)"
  unset _q_common _q_dir _st
fi

# The spoke is live: recording its dispatch id is best-effort and never fails the spawn.
if [ -n "$DISPATCH_ID" ]; then
  _wt_provision --identity-only --orca-dispatch-id "$DISPATCH_ID" \
    || wt_warn "could not record the dispatch id in .ai-toolkit/identity"
else
  wt_warn "worker-start reported no dispatch id; .ai-toolkit/identity keeps it empty"
fi

# The one-shot preflights above only cover the spawn instant; the watchdog
# daemon keeps the collector+bridge alive for the whole spoke lifetime (machine
# sleep/wake, #138) and exits itself when the last spoke agent is gone. Armed
# AFTER the launch so its first tick already sees the live agent;
# best-effort and self-gating (no-op unless AI_TOOLKIT_OTEL=1, singleton).
wt_otel_watch_arm "$REPO_ROOT"

# --- GitHub lifecycle-label mirror: dispatch (issue #236) --------------------
# Stamp the issue so its GitHub list entry shows the spoke is live: status:in-progress
# + mode:<attended|afk> + lane:spoke, plus a one-time dispatch comment linking the
# issue back to the branch / worktree / Orca worktree id / spoke_run_id (and thus its
# Langfuse session). Numbered issues only — an ad-hoc slug carries no issue, so the
# express/quick/micro lanes mirror nothing by construction. Every write is
# best-effort and time-bounded inside the wt_gh_* helpers, so a failed / hung /
# absent / opted-out gh never fails the spawn.
# UPGRADE: correcting the mode label of an ALREADY-attended spoke when a drain
# arms mid-run is left to a follow-up — dispatch stamps mode once, and hub-afk
# passes --mode afk for drain-dispatched spokes so those are correct at spawn.
if [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  wt_gh_apply_dispatch_labels "$ISSUE" "$MODE" "$LANE"
  wt_gh_dispatch_comment "$ISSUE" "$(printf 'Dispatched — spoke is live (issue #236 lifecycle mirror).\n- branch: %s\n- worktree: %s\n- orca worktree: %s\n- spoke_run_id: %s' \
    "$BRANCH" "$WT_DIR" "$WT_ID" "$SPOKE_RUN_ID")"
fi

# --- telemetry: spawn lifecycle marker + script run-node ---------------------
# Attributed to the new spoke (emitted with the worktree as CWD), carrying the
# spoke_run_id minted above. The script span is this control script as a trace
# node; it shares its name with the lifecycle marker (emission-link basis). No-op
# unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-new" "spawn" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "worktree-new" "success" "$WT_T0" "$WT_DIR"

echo
echo "✓ dispatched: $WT_DIR"
echo "  branch:     $BRANCH"
echo "  Task contract on disk:  .ai-toolkit/task.md  (crash re-anchor: /source-task $ISSUE)"

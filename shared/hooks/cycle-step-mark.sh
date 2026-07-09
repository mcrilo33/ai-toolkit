#!/usr/bin/env bash
# cycle-step-mark.sh — PostToolUse hook (Claude): derive the solo-cycle step
# markers from the mechanical witness of each transition, so cycle-step telemetry
# no longer depends on the LLM narrating it (Issue #178).
#
# Witness -> phase:
#   a `git commit` whose HEAD message carries the `Tested-RED:` trailer  -> red
#   a `git commit` whose HEAD message does not (and is not a merge)      -> green
#   a `git push` that left the branch's `@{upstream}` at HEAD             -> push
#   a Write of a worktree-root `.review/<name>.json` artifact            -> review
#
# SCOPE — cycle steps belong to a solo-cycle running in a SPOKE. WT_SPOKE is the
# mechanical spoke-role signal (set by worktree-new.sh); the hub and /quick lanes
# do not set it, so their commits/pushes never mint phantom cycle steps. This is
# the context guard the #139 emitters get implicitly by only running on real
# cycle/ship paths; this hook fires on every commit/push, so it gates explicitly.
#
# Every emission routes through telemetry_mark_cycle_step, which is idempotent per
# (phase, HEAD sha): it dedupes with the #139 gate emitters that mark the same
# phases from their own paths — the review-stamp guard (review) and spoke-push.sh
# (push) — so this hook is belt-and-suspenders, never a double span. It also fills
# the gaps those gates miss: RED/GREEN had no explicit marker at all, an Agent-tool
# code-review never touches the review-stamp MCP, and a mid-cycle `git push` never
# runs spoke-push.sh.
#
# PostToolUse fires only AFTER the tool ran, so a commit or push a PreToolUse gate
# denied produces no witness and no marker; a git-level failure (rejected push,
# empty commit) leaves HEAD/@{upstream} unchanged, and the HEAD-keyed idempotency
# skips the already-emitted marker rather than fabricating a new one.
#
# UPGRADE: a `git -C <other-repo> commit` is classified from THIS worktree's HEAD
# (is_git_commit matches the -C form, but the root/HEAD resolve to the current
# worktree). Harmless under dedup unless the current HEAD is unmarked; solving it
# would mean parsing the -C target and re-rooting — not worth it for a case a spoke
# essentially never hits (spoke-main-guard blocks most cross-repo ref work).
#
# TELEMETRY-ONLY + INVISIBLE — gated on a live sink; no stdout (no permission
# decision, so it composes with the deny/allow guards), never changes the exit
# code. Like parent-span-export this hook fires on EVERY Bash and Write call, so it
# must NOT arm the per-hook kind=hook span — that would be pure noise. It reuses the
# boundary-aware git-command classifiers from utils.sh, so rather than sidestep the
# lib it pre-arms the span guard (_TELEMETRY_HOOK_ARMED) before sourcing, which makes
# telemetry_arm_hook_span a no-op.

set -euo pipefail

# Telemetry opt-in gate. On the common non-witness call, touch nothing before any
# lib is sourced or any subprocess runs.
[ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || [ -n "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-}" ] || exit 0

# Spoke-role gate (see header): only a spoke runs solo-cycles.
[ -n "${WT_SPOKE:-}" ] || exit 0

# Without jq we cannot safely read the payload; degrade to a no-op.
command -v jq >/dev/null 2>&1 || exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Suppress this hook's per-call kind=hook span (see header): pre-arm the guard so
# the telemetry_arm_hook_span that utils.sh runs at source time no-ops.
_TELEMETRY_HOOK_ARMED=1
# shellcheck source=lib/utils.sh
source "$HOOK_DIR/lib/utils.sh"

# Read the payload once at script scope so _telemetry_project_root (and the emit
# layer) resolve the worktree root / spoke_run_id from it.
INPUT=$(read_stdin)
[ -n "$INPUT" ] || exit 0

# Emit one marker, idempotent per (phase, key). Guarded like the #139 callers so a
# missing emit layer can never fail the hook.
_mark() {
  command -v telemetry_mark_cycle_step >/dev/null 2>&1 || return 0
  telemetry_mark_cycle_step "$1" "${2:-}"
}

TOOL_NAME=$(get_tool_name "$INPUT")

case "$TOOL_NAME" in
  Bash)
    CMD=$(get_shell_command "$INPUT")
    # Resolve the worktree HEAD once, shared by the commit and push witnesses. An
    # unresolvable HEAD (unborn branch, failed first commit, misresolved root) is
    # not a witness for either — exit rather than default a phantom marker.
    if is_git_commit "$CMD" || is_git_push "$CMD"; then
      root=$(_telemetry_project_root)
      # --verify: fail cleanly (empty stdout) on an unborn branch — a bare
      # `rev-parse HEAD` echoes the literal "HEAD" to stdout on failure.
      head=$(git -C "$root" rev-parse -q --verify HEAD 2>/dev/null || true)
      [ -n "$head" ] || exit 0

      # Commit and push are checked independently (not either/or): a compound
      # `git commit -m x && git push` is a single Bash call carrying BOTH witnesses.
      if is_git_commit "$CMD"; then
        # A merge commit (2+ parents) is reconciliation (`git merge origin/main`),
        # not a cycle step — skip it.
        if ! git -C "$root" rev-parse -q --verify "${head}^2" >/dev/null 2>&1; then
          # red vs green from the `Tested-RED:` TRAILER (keyword + colon), the same
          # machine-read token the commit cage keys on — not a whole-body substring,
          # so prose that merely mentions "Tested-RED" does not force a false red.
          if git -C "$root" log -1 --format=%B "$head" 2>/dev/null \
            | grep -qE '(^|[[:space:]])Tested-RED:'; then
            _mark red "$head"
          else
            _mark green "$head"
          fi
        fi
      fi

      if is_git_push "$CMD"; then
        # The push witness is the branch's upstream sitting at HEAD (the branch is
        # pushed to this commit). A `gh pr`/tag/no-op push is excluded by is_git_push
        # and this check; idempotency keys on HEAD so a re-push emits nothing.
        upstream=$(git -C "$root" rev-parse '@{upstream}' 2>/dev/null || true)
        [ -n "$upstream" ] && [ "$head" = "$upstream" ] && _mark push "$head"
      fi
    fi
    ;;
  Write)
    FILE=$(get_file_path "$INPUT")
    [ -n "$FILE" ] || exit 0
    root=$(_telemetry_project_root)
    # A new review artifact at the WORKTREE-ROOT .review/ dir (never a nested
    # `.../.review/` elsewhere in the tree, and never the .review/.window sentinel,
    # which is not a .json). Normalize to an absolute path first so both the
    # relative (cwd == root) and absolute payload forms anchor to `$root/.review/`.
    case "$FILE" in
      /*) abs="$FILE" ;;
      *) abs="$PWD/$FILE" ;;
    esac
    case "$abs" in
      "$root"/.review/*.json) _mark review ;;
    esac
    ;;
esac

exit 0

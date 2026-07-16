#!/usr/bin/env bash
#
# worktree-lib.sh — shared helpers for worktree-new.sh and worktree-done.sh.
# Source this file; do not execute it. Callers set WT_PROG to their program name
# so diagnostics are attributed correctly.
#
# The two scripts MUST agree on slugify rules and on how a user-supplied target
# resolves to a worktree, so that anything you can create you can also tear down.
# Keeping that logic here is what guarantees it.

# --- diagnostics --------------------------------------------------------------

wt_die()  { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; exit 1; }
wt_warn() { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; }

# --- telemetry (opt-in, optional) ---------------------------------------------
# Source the shared span emit layer if present, so the worktree scripts can emit
# lifecycle spans. It is self-contained and gated by AI_TOOLKIT_TELEMETRY=1, so
# sourcing it is a no-op when telemetry is off. Locate it relative to THIS lib:
# in the ai-toolkit checkout it lives under shared/hooks/lib/; in a synced target
# the sync co-locates it next to these scripts in .ai-toolkit/scripts/.
_WT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _c in "$_WT_LIB_DIR/telemetry.sh" "$_WT_LIB_DIR/../shared/hooks/lib/telemetry.sh"; do
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
unset _c

# --- base branch (issue #117) ---------------------------------------------------
# The canonical wt_base_branch resolver is defined ONCE in
# shared/hooks/lib/base-branch.sh (the guard hooks source it from their lib/),
# and re-exported here for every worktree/hub script. Same two-layout candidate
# resolution as telemetry.sh above: co-located in a synced target, under
# shared/hooks/lib/ in the ai-toolkit checkout.
for _c in "$_WT_LIB_DIR/base-branch.sh" "$_WT_LIB_DIR/../shared/hooks/lib/base-branch.sh"; do
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
unset _c

# The ref new spokes branch FROM for the repo at $1 (default "."): origin/<base>
# when the remote ref exists (the hub's local base may lag or carry unpushed
# work), else the local <base>. Fails (rc 1, no output) when the resolved base
# exists nowhere — a config typo must die at the call site, never silently
# branch from something else.
wt_base_start_point() {
  local root="${1:-.}" base
  base="$(wt_base_branch "$root")"
  if git -C "$root" show-ref --verify --quiet "refs/remotes/origin/$base" 2>/dev/null; then
    printf 'origin/%s' "$base"
    return 0
  fi
  if git -C "$root" show-ref --verify --quiet "refs/heads/$base" 2>/dev/null; then
    printf '%s' "$base"
    return 0
  fi
  return 1
}

# Emit one lifecycle span for a worktree action, attributing it to the SPOKE:
# run the emit with the worktree as CWD so the span resolves that worktree's
# spoke_run_id / branch / repo. No-op when the emit layer or telemetry is absent.
# Usage: wt_emit_lifecycle <name> <phase> <status> <start_ms> <worktree_dir>
wt_emit_lifecycle() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local name="$1" phase="$2" status="$3" start_ms="$4" wt="$5"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind lifecycle --name "$name" \
      --phase "$phase" --status "$status" --start-ms "$start_ms" ) || true
  return 0
}

# Emit one kind=script run-node span for a worktree script, attributing it to the
# SPOKE the same way wt_emit_lifecycle does (worktree as CWD). This is the control
# script as a first-class trace node (Issue #54); it shares its name with the
# lifecycle marker so the parser can later link marker→script via `emits`. The
# `emits` link stays null on push. No-op when the emit layer or telemetry is absent.
# Usage: wt_emit_script <name> <status> <start_ms> <worktree_dir>
wt_emit_script() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local name="$1" status="$2" start_ms="$3" wt="$4"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind script --name "$name" \
      --status "$status" --start-ms "$start_ms" ) || true
  return 0
}

# Epoch-ms clock for span start times; empty string when the emit layer is
# absent (callers pass it through to wt_emit_lifecycle, which then defaults).
wt_now_ms() {
  command -v _telemetry_now_ms >/dev/null 2>&1 && _telemetry_now_ms || true
}

# Build the native-OTel launch env PREFIX (issue #83, reused by relaunch #233).
# Emits the exact env prefix a spoke's `claude` launch carries so the run streams
# one nested trace grouped by its spoke_run_id (carried in OTEL_RESOURCE_ATTRIBUTES).
# Empty string when native OTel is opted out (AI_TOOLKIT_OTEL != 1) — the full
# opt-out. The non-secret connection endpoints default to the local collector; an
# operator override in the env is preserved (`:=`). The trailing space is
# load-bearing: it separates this prefix from the WT_SPOKE pin the caller appends.
# The AUTH header (OTEL_EXPORTER_OTLP_HEADERS) is deliberately NOT emitted — it stays
# inherited env, off the command line (visible in ps/tmux). SINGLE SOURCE for both
# worktree-new.sh (spawn) and spoke-relaunch.sh (relaunch), so the two never drift.
# The body dir is created by the caller (it also passes it to the bridge preflight).
# Usage: wt_native_otel_prefix <spoke_run_id> <body_dir>
# Default AI_TOOLKIT_OTEL_SPAN_ENDPOINT in the CALLER'S shell (unset -> the local
# collector's OTLP-HTTP :4318, operator override preserved) and EXPORT it. The single
# source for this default (#233): the span sink in telemetry.sh's emit reads it from the
# emitting shell, so worktree-new.sh (spawn) and spoke-relaunch.sh (relaunch) each call
# this before their wt_emit_* calls; wt_native_otel_prefix calls it too so the prefix
# string's value matches. Must run non-subshelled to set the caller's var.
wt_default_span_endpoint() {
  : "${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:=${AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT:-http://localhost:4318}}"
  export AI_TOOLKIT_OTEL_SPAN_ENDPOINT
}

wt_native_otel_prefix() {
  local spoke_run_id="$1" body_dir="$2"
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  # Default the non-secret endpoints when unset (operator override preserved); the
  # normal gRPC :4317 / beta HTTP :4418 split is load-bearing — a beta endpoint on the
  # normal host:port silently kills trace+log export. The span sink is OTLP-HTTP :4318.
  : "${OTEL_EXPORTER_OTLP_ENDPOINT:=http://localhost:4317}"
  : "${BETA_TRACING_ENDPOINT:=http://localhost:4418}"
  wt_default_span_endpoint
  printf 'CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp ENABLE_BETA_TRACING_DETAILED=1 OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=%s BETA_TRACING_ENDPOINT=%s AI_TOOLKIT_OTEL_SPAN_ENDPOINT=%s OTEL_LOG_USER_PROMPTS=1 OTEL_LOG_TOOL_DETAILS=1 OTEL_LOG_TOOL_CONTENT=1 OTEL_LOG_RAW_API_BODIES=%s AI_TOOLKIT_OTEL_BODY_DIR=%s OTEL_RESOURCE_ATTRIBUTES=%s ' \
    "$(printf '%q' "$OTEL_EXPORTER_OTLP_ENDPOINT")" \
    "$(printf '%q' "$BETA_TRACING_ENDPOINT")" \
    "$(printf '%q' "$AI_TOOLKIT_OTEL_SPAN_ENDPOINT")" \
    "$(printf '%q' "file:${body_dir}")" \
    "$(printf '%q' "$body_dir")" \
    "$(printf '%q' "spoke_run_id=${spoke_run_id}")"
}

# Resolve the spoke driver's default model + effort (issue #142), layered so it works
# both in a synced target AND in the hub:
#   1. the sync-emitted spoke-model.env co-located with the caller script, else
#   2. the hub's config via ai_toolkit_config.py's `spoke-env` seam, else
#   3. the historical literal defaults.
# Sets WT_AGENT_MODEL / WT_AGENT_EFFORT in the CALLER'S shell (it is sourced, not run in
# a subshell), so an explicit env / `Model:` override the caller already exported always
# wins — the layers only fill an unset value. SINGLE source shared by worktree-new.sh
# (spawn) and spoke-relaunch.sh (relaunch) so a relaunched spoke never runs on a
# different/stale model than a freshly-spawned one for the same issue.
# Usage: wt_resolve_agent_model <script_dir> <config_path>
wt_resolve_agent_model() {
  local script_dir="$1" config="$2"
  if [ -f "$script_dir/spoke-model.env" ]; then
    # shellcheck disable=SC1091
    . "$script_dir/spoke-model.env"
  elif [ -f "$script_dir/ai_toolkit_config.py" ] && [ -f "$config" ]; then
    eval "$(python3 "$script_dir/ai_toolkit_config.py" spoke-env "$config" 2>/dev/null || true)"
  fi
  # Literal fallback when neither spoke-model.env nor the config is readable (a
  # config-less self-copy — the drain's temp copy omits spoke-model.env on every
  # recycle, #306). This value is what a spoke ACTUALLY launches on in that case, so
  # it must be a SANE default, not the priciest tier: an earlier `claude-opus-4-8[1m]`
  # / `max` literal silently dispatched the drain's spokes on the 1M premium tier
  # whenever the self-copy lacked the config (observed thrice: the budget-routing
  # land, #291, #305). A silent fallback to the MOST expensive option is the worst
  # shape a default can have. `claude-opus-4-8` (no 1m) / `high` matches the intended
  # driver default and keeps compaction on. When the config IS present it wins, so
  # a Sonnet/budget posture is still honored — this only bounds the config-less case.
  if [ -z "${WT_AGENT_MODEL:-}" ] && [ -z "${WT_AGENT_MODEL_DEFAULT:-}" ]; then
    wt_warn "spoke model config not found (no spoke-model.env / config) — using the safe default claude-opus-4-8/high; a synced target or seeded self-copy should override this (see #306)" || true
  fi
  WT_AGENT_MODEL="${WT_AGENT_MODEL:-${WT_AGENT_MODEL_DEFAULT:-claude-opus-4-8}}"
  WT_AGENT_EFFORT="${WT_AGENT_EFFORT:-${WT_AGENT_EFFORT_DEFAULT:-high}}"
}

# --- client-side telemetry config resolution (issue #228) -----------------------
# wt_resolve_telemetry_config [config-path] -> read the NON-SECRET client-side
# telemetry settings from settings/ai-toolkit.yml (via ai_toolkit_config.py's
# telemetry-env seam) and set the *_DEFAULT shell vars the consumers layer behind a
# live env override (env -> config -> hardcoded default). Only keys the operator set
# are emitted, so an un-migrated (telemetry-less) config leaves every default unset
# and the consumer keeps its own literal. Best-effort and never fatal: a missing
# python or config file is a silent no-op. The path defaults to the ai-toolkit
# checkout's settings/ai-toolkit.yml (this lib's ../settings sibling); AI_TOOLKIT_CONFIG
# overrides it, matching sync-to-repo.sh / worktree-new.sh. A synced target carries
# no such file, so it no-ops there and telemetry stays env-or-default driven. The
# SECRET (LANGFUSE_BASIC_AUTH) is never in the config — it stays in ~/.afk-telemetry,
# resolved by wt_resolve_langfuse_auth below.
wt_resolve_telemetry_config() {
  local cfg="${1:-${AI_TOOLKIT_CONFIG:-$_WT_LIB_DIR/../settings/ai-toolkit.yml}}"
  local pyc="$_WT_LIB_DIR/ai_toolkit_config.py"
  [ -f "$pyc" ] && [ -f "$cfg" ] || return 0
  # eval sets AI_TOOLKIT_OTEL_DEFAULT / LANGFUSE_HOST_DEFAULT /
  # AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT / LANGFUSE_PROJECT_DEFAULT /
  # LANGFUSE_PUBLIC_KEY_DEFAULT for the keys the config set; a parse failure or an
  # empty section yields no assignment, so this never clobbers a live value.
  eval "$(python3 "$pyc" telemetry-env "$cfg" 2>/dev/null || true)"
}

# --- hub-side Langfuse auth resolution (issue #127) -----------------------------
# wt_resolve_langfuse_auth -> resolve LANGFUSE_BASIC_AUTH the way hub-afk.sh's
# afk_resolve_telemetry_auth does — env first, then the SAME optional conf file
# (${AFK_TELEMETRY_CONF:-~/.afk-telemetry}), each field independently (the
# save-source-restore precedence) — so a land or /quick from ANY hub session can
# reach Langfuse without the operator hand-exporting credentials. On success,
# EXPORT the auth + host (for the post-run ingesters) and default + export
# AI_TOOLKIT_OTEL_SPAN_ENDPOINT to the local collector's OTLP-HTTP port, which is
# what lets telemetry.sh's script-span fan-out fire for hub-side scripts; the
# span POST itself carries no credential (the collector holds auth), and its curl
# is detached + swallowed, so a down collector costs nothing. rc 1 and NO exports
# when no auth resolves — callers stay on their existing skip-WARN paths. The afk
# resolver stays as-is (its arming semantics differ: it also exports
# AI_TOOLKIT_OTEL=1); deduplicating the two is a follow-up once both land.
wt_resolve_langfuse_auth() {
  local conf="${AFK_TELEMETRY_CONF:-$HOME/.afk-telemetry}"
  # Client-side telemetry defaults (host/project/public_key/endpoint) from
  # settings/ai-toolkit.yml (issue #228); sets the *_DEFAULT vars used as the final
  # fallback below. The SECRET auth still comes from env / the conf file, never here.
  wt_resolve_telemetry_config
  if [ -f "$conf" ]; then
    local s_auth="${LANGFUSE_BASIC_AUTH:-}" s_host="${LANGFUSE_HOST:-}" \
          s_ep="${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-}" \
          s_proj="${LANGFUSE_PROJECT:-}" s_pk="${LANGFUSE_PUBLIC_KEY:-}"
    # shellcheck disable=SC1090
    . "$conf" 2>/dev/null || true
    [ -n "$s_auth" ] && LANGFUSE_BASIC_AUTH="$s_auth"
    [ -n "$s_host" ] && LANGFUSE_HOST="$s_host"
    [ -n "$s_ep" ] && AI_TOOLKIT_OTEL_SPAN_ENDPOINT="$s_ep"
    # Project + public key are env-wins per field too (issue #228), so restore an
    # env value the conf may have overwritten before the config default applies below.
    [ -n "$s_proj" ] && LANGFUSE_PROJECT="$s_proj"
    [ -n "$s_pk" ] && LANGFUSE_PUBLIC_KEY="$s_pk"
  fi
  [ -n "${LANGFUSE_BASIC_AUTH:-}" ] || return 1
  export LANGFUSE_BASIC_AUTH
  # env -> ~/.afk-telemetry conf (set above) -> config default -> hardcoded literal.
  export LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_HOST_DEFAULT:-http://localhost:3000}}"
  export AI_TOOLKIT_OTEL_SPAN_ENDPOINT="${AI_TOOLKIT_OTEL_SPAN_ENDPOINT:-${AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT:-http://localhost:4318}}"
  # Project + public key are both PUBLIC; export the config default only when set,
  # and a live env value still wins. Guarded (not `&& export`) so an unset default
  # can't make the whole function return non-zero on an otherwise-successful resolve.
  if [ -n "${LANGFUSE_PROJECT_DEFAULT:-}" ]; then
    export LANGFUSE_PROJECT="${LANGFUSE_PROJECT:-$LANGFUSE_PROJECT_DEFAULT}"
  fi
  if [ -n "${LANGFUSE_PUBLIC_KEY_DEFAULT:-}" ]; then
    export LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-$LANGFUSE_PUBLIC_KEY_DEFAULT}"
  fi
  return 0
}

# --- SSH-keepalive push (issue #119) --------------------------------------------
# The pre-push suite (~6 min) runs INSIDE `git push`, between the SSH connection
# opening and the pack transfer. GitHub reaps the idle connection mid-gate, so a
# fully green push dies in the transfer phase (exit 141 / "closed by remote
# host"). Every worktree/spoke push routes through wt_git_push so the connection
# is kept alive across the gate.

WT_SSH_KEEPALIVE_OPTS="-o ServerAliveInterval=15 -o ServerAliveCountMax=40"

# The ssh command for keepalive pushes: a pre-existing GIT_SSH_COMMAND (custom
# binary, -i identity, -o options) is preserved verbatim as the prefix and the
# keepalive options are APPENDED — OpenSSH honors the first occurrence of an
# option, so a caller's own ServerAlive* settings keep winning.
wt_git_ssh_command() {
  printf '%s %s' "${GIT_SSH_COMMAND:-ssh}" "$WT_SSH_KEEPALIVE_OPTS"
}

# `git push "$@"` with the keepalive GIT_SSH_COMMAND, scoped to the one git
# process (a leading assignment — the caller's env is untouched).
wt_git_push() {
  GIT_SSH_COMMAND="$(wt_git_ssh_command)" git push "$@"
}

# wt_push_transport_died <push-exit-code> <captured-output-file> — did a failed
# push die at the TRANSPORT layer? git only enters the transfer phase after the
# pre-push hook exits 0, so the named signatures are proof the gate ran green.
# worktree-land uses that — TOGETHER WITH a positive green-tree stamp for the
# pushed tree (wt_gate_green_stamped, issue #214) — to retry once with the suite
# skipped. The bare exit 141 (SIGPIPE) is deliberately KEPT here as a transport
# candidate even though a gate killed mid-run can share it: the caller no longer
# trusts 141 alone, requiring the green stamp a killed gate never leaves. A
# failed gate (pytest output + git's local refusal) matches nothing here —
# deliberately no bare "broken pipe" pattern, which a BrokenPipeError traceback
# in pytest output would fake.
wt_push_transport_died() {
  [ "${1:-0}" -eq 141 ] && return 0
  grep -qiE \
    'closed by remote host|packet_write_wait|client_loop: send disconnect|remote end hung up unexpectedly|send-pack: unexpected disconnect' \
    "$2" 2>/dev/null
}

# wt_gate_green_stamped — 0 iff a GREEN-TREE stamp (issue #122) EXISTS for THIS
# tree's HEAD^{tree}. It is the positive proof worktree-land requires before
# honoring a transport retry (issue #214): a stamp is written ONLY by a passing
# gate (test-select.sh mints on rc==0), so its presence proves this exact tree
# ran green at some tier for some runner, whereas a gate KILLED mid-run
# (SIGPIPE/OOM — exit 141, no pytest summary) never reaches its mint. The retry-
# with-suite-skipped is refused absent such a stamp. shared/hooks/lib/gate-stamp.sh
# is the authoritative WRITER; this reader mirrors its placement contract
# (<git-common-dir>/.gate-stamps/ keyed by HEAD^{tree}) WITHOUT sourcing it, so
# the READER path resolves identically in the ai-toolkit checkout and a synced
# downstream hub (which carries no shared/hooks/lib/).
#
# Scope of the proof (deliberately weaker than the writer's gate_stamp_check):
#   • EXISTENCE only — no tier/runner-fingerprint match. A pre-existing stamp
#     that does not COVER the current demand (a weaker tier, a different runner)
#     is accepted here even though the gate would not have consumed it. The tree
#     is still proven green (at that other tier/env), so this is bounded — never
#     "ship an untested tree" — and matches what a clean-FF land already trusts
#     via AUTO_SKIP (issue #96). It is NOT a full re-verification.
#   • The key is HEAD^{tree} alone — NOT gated on a clean working tree the way
#     the writer's gate_stamp_tree is. The push does not move HEAD, so HEAD^{tree}
#     is exactly the pushed tree; keeping the clean check would let the gate's own
#     .testmondata* writes read the tree "dirty" post-push and deny a legitimate
#     retry.
# Fail-CLOSED on absence: an unborn HEAD or a missing stamp returns non-zero, so
# the retry rolls back. This means the #119 keepalive retry is disabled whenever
# no stamp was minted — an untracked file on the hub (the land tolerates it via
# `git status --porcelain -uno`, but the writer's untracked-sensitive
# gate_stamp_tree skips the mint) or a pre-#122 installed hook (STAMPS=0). Those
# are the safe direction: re-running the land re-runs the gate.
# _wt_gate_stamp_file — print the stamp path for HEAD^{tree}: the #122 contract
# <git-common-dir>/.gate-stamps/<HEAD^{tree}>, resolved WITHOUT sourcing gate-stamp.sh
# so a synced hub (no shared/hooks/lib/) resolves it identically. rc 1 (no output) on
# an unborn HEAD or an unresolvable common dir. Shared by both stamp readers below.
_wt_gate_stamp_file() {
  local common tree
  tree="$(git rev-parse -q --verify 'HEAD^{tree}' 2>/dev/null)" || return 1
  [ -n "$tree" ] || return 1
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  case "$common" in /*) ;; *) common="$PWD/$common" ;; esac
  printf '%s/.gate-stamps/%s' "$common" "$tree"
}

wt_gate_green_stamped() {
  local stamp
  stamp="$(_wt_gate_stamp_file)" || return 1
  [ -f "$stamp" ]
}

# wt_gate_green_stamped_fresh <max_age_seconds> — like wt_gate_green_stamped, but
# additionally requires the stamp to be YOUNGER than <max_age_seconds> by file mtime.
# A tree-identical fast-forward land (issue #270) consults this to reuse a RECENT
# green proof instead of re-running the gate on a byte-identical tree. Existence-only
# is the same bounded trust a clean-FF land already extends via AUTO_SKIP (issue #96)
# — the tree WAS proven green (at some tier/env) — and the freshness bound guards
# against env drift (a new runner/dep since the proof) waving through a tree proven
# long ago. Fail-CLOSED: a missing stamp, an unreadable/absent mtime, a future mtime
# (clock skew), or an age beyond the bound all return non-zero, so the land re-runs
# the gate. The mtime read is numeric (epoch seconds), so it needs no LC_ALL pin.
wt_gate_green_stamped_fresh() {
  local max_age="$1" stamp now mtime age
  [ -n "$max_age" ] || return 1
  stamp="$(_wt_gate_stamp_file)" || return 1
  [ -f "$stamp" ] || return 1
  now="$(date +%s 2>/dev/null)" || return 1
  mtime="$(_wt_file_mtime "$stamp")" || return 1
  [ -n "$mtime" ] || return 1
  age=$(( now - mtime ))
  [ "$age" -ge 0 ] || return 1          # future mtime (clock skew) → fail closed
  [ "$age" -le "$max_age" ]
}

# --- portable date/time -------------------------------------------------------
# BSD (macOS) and GNU date differ; try the BSD form first, fall back to GNU.
# Kept here so the unattended supervisor (hub-afk.sh) and any future caller share
# one copy of the date/time helpers.

# _wt_file_mtime <path> -> the file's mtime in epoch seconds. GNU `stat -c %Y` first
# (Linux/CI), BSD `stat -f %m` as the fallback (macOS dev host): the GNU form errors
# on BSD stat (unknown -c) so the || cleanly selects the right one, the mirror of the
# BSD-first date helpers below. rc 1 (no output) when the path is unreadable.
_wt_file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

# wt_date_ymd <epoch> -> YYYY-MM-DD (local time).
wt_date_ymd() {
  date -r "$1" +%Y-%m-%d 2>/dev/null || date -d "@$1" +%Y-%m-%d
}

# wt_epoch_at <yyyy-mm-dd> <hh:mm> -> epoch seconds (local time).
# Seconds are pinned to :00 explicitly: BSD `date -j -f` fills a missing %S field
# from the current wall clock, which would leak the invocation second into the
# result and could flip a one-minute cutoff/window decision.
wt_epoch_at() {
  date -j -f "%Y-%m-%d %H:%M:%S" "$1 $2:00" +%s 2>/dev/null || date -d "$1 $2" +%s
}

# --- paths --------------------------------------------------------------------

# Canonical absolute path (resolves symlinks, e.g. /tmp -> /private/tmp on macOS).
# Empty output if the path does not exist.
wt_realpath() { (cd "$1" 2>/dev/null && pwd -P) || true; }

# Absolute, canonical path of the MAIN worktree — the first entry of
# `git worktree list`. Correct even when called from inside a linked worktree,
# which is why both scripts use this instead of `git rev-parse --show-toplevel`.
wt_main_root() {
  local p
  p="$(git worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')"
  [ -n "$p" ] || return 1
  wt_realpath "$p"
}

# wt_marker_script_dir <root> -> the relative dir under <root> where the canonical marker
# emitters (spoke-ready.sh / spoke-push.sh) live and are RUNNABLE: `scripts` in the ai-toolkit
# checkout (tracked, so `git worktree add` checks them out), else `.ai-toolkit/scripts` (a synced
# target's gitignored sync dir). The seed prompt / allowlist (worktree-new.sh) and the /afk nudge
# (hub-afk.sh) must name the path that actually EXISTS in the spawned worktree — a mismatch is
# denied at the deny-wall AND fails to exec, the #271 phantom-park incident. Defaults to the
# synced-target path (the historical hardcoded value) when neither is resolvable.
wt_marker_script_dir() {
  local root="$1"
  if [ -f "$root/scripts/spoke-ready.sh" ]; then
    printf 'scripts\n'
  else
    printf '.ai-toolkit/scripts\n'
  fi
}

# --- review workspace file (issue #134) ----------------------------------------
# The VS Code review "window" is a saved .code-workspace file. `code --add` /
# `code --remove` target the *last-focused* window and routinely miss (landed
# spokes ghosting in the Explorer, live spokes never added), so creation and
# teardown edit the file's `folders` array directly — VS Code hot-reloads it.
# The editors return 1 on a missing or unparseable file (e.g. hand-edited JSONC:
# strict JSON is the price of a safe rewrite) so callers fall back to the legacy
# `code` CLI path; in that case the file is never rewritten or truncated.

# wt_workspace_file <repo_root> -> path of the review workspace file.
# `git config ai-toolkit.workspace-file` wins (leading ~ expanded — git stores
# the value verbatim); default ~/.claude/<repo-basename>.code-workspace so
# synced target repos each get their own review workspace.
wt_workspace_file() {
  local cfg
  cfg="$(git -C "$1" config ai-toolkit.workspace-file 2>/dev/null || true)"
  if [ -z "$cfg" ]; then
    printf '%s/.claude/%s.code-workspace\n' "$HOME" "$(basename "$1")"
  else
    case "$cfg" in
      "~/"*) printf '%s/%s\n' "$HOME" "${cfg#\~/}" ;;
      *)     printf '%s\n' "$cfg" ;;
    esac
  fi
}

# Shared driver for the two editors below. Entries resolve against the
# workspace file's directory (relative paths are relative to it); the rewrite
# is atomic (unique tmp + rename), serialized across concurrent worktree ops by
# an flock on a sidecar .lock (spawns/teardowns overlap under /next-batch and
# /afk — a bare read-modify-write loses entries), tab-indented like VS Code's
# own writes, and skipped entirely when nothing changed.
wt_workspace_edit() {
  python3 - "$1" "$2" "$3" <<'PY'
import fcntl
import json
import os
import sys
import tempfile

op, ws_file, wt_dir = sys.argv[1], sys.argv[2], sys.argv[3]
if not os.path.isfile(ws_file):
    sys.exit(1)

# One lock for every editor of this workspace file. A sidecar (never replaced)
# rather than the file itself: os.replace swaps the inode, so a waiter holding
# the old fd would lock an orphan and read stale content.
lock_fd = os.open(ws_file + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(lock_fd, fcntl.LOCK_EX)

text = open(ws_file).read()
try:
    doc = json.loads(text)
    folders = doc["folders"]
    if not isinstance(folders, list):
        raise ValueError("'folders' is not a list")
except (ValueError, KeyError, TypeError) as e:
    print(f"worktree: unparseable workspace file {ws_file} ({e}) — "
          "falling back to the `code` CLI, file left untouched", file=sys.stderr)
    sys.exit(1)

# Canonicalize BOTH ends before relpath: with only the target realpath'd, a
# symlinked ancestor of the workspace file (NFS/corp homes) yields a lexical
# `..`-chain that resolves to a nonexistent physical path — VS Code shows a
# broken folder and the next sweep would drop the live entry.
ws_dir = os.path.dirname(os.path.realpath(ws_file))
target = os.path.realpath(wt_dir)


def resolve(entry):
    """Absolute, canonical path of a folder entry; None when it has no path."""
    p = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(p, str) or not p:
        return None
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(ws_dir, p)
    return os.path.realpath(p)


if op == "add":
    if any(resolve(e) == target for e in folders):
        sys.exit(0)  # already present — never duplicate, never rewrite
    folders.append(
        {"name": os.path.basename(target), "path": os.path.relpath(target, ws_dir)}
    )
else:  # remove — and sweep ghosts of past misses in the same pass
    kept = []
    for e in folders:
        resolved = resolve(e)
        if resolved is None:
            kept.append(e)  # path-less entry — cannot judge, conservatively kept
        elif resolved == target or not os.path.exists(resolved):
            continue
        else:
            kept.append(e)
    if kept == folders:
        sys.exit(0)  # nothing to drop — don't churn the file
    doc["folders"] = kept

fd, tmp = tempfile.mkstemp(prefix=os.path.basename(ws_file) + ".", dir=ws_dir)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent="\t", ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, ws_file)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
PY
}

# wt_workspace_add <ws_file> <wt_dir> -> 0 entry present (appended or already
# there); 1 missing/unparseable file (caller falls back to `code --add`).
# Call in a conditional (`if`/`||`) — a bare call aborts a `set -e` caller
# before the fallback can run.
wt_workspace_add() { wt_workspace_edit add "$1" "$2"; }

# wt_workspace_remove <ws_file> <wt_dir> -> 0 entry absent (removed, swept, or
# never there); 1 missing/unparseable file (caller falls back to `code --remove`).
# Same `set -e` caveat as wt_workspace_add: only call in a conditional.
wt_workspace_remove() { wt_workspace_edit remove "$1" "$2"; }

# --- slug ---------------------------------------------------------------------

# Lowercase, collapse non-alphanumeric runs to '-', strip edges, keep <=4 segments.
# Both creation and teardown run identical input through this, so a raw arg
# normalizes the same way on both sides.
wt_slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -d- -f1-4
}

# --- tmux session name --------------------------------------------------------

# Derive a stable, tmux-safe session name for a repo root: parent-dir prefix +
# basename ('<parent>-<base>'), so two repos sharing a basename under different
# parents get distinct sessions and 'tmux ls' reads as a per-project portfolio.
# tmux forbids '.' and ':' in session names → map them to '-'. The caller passes
# the canonical main-worktree root, so the result is deterministic per repo.
wt_tmux_session() {
  local root="$1" parent base
  parent="$(basename "$(dirname "$root")")"
  base="$(basename "$root")"
  printf '%s-%s' "$parent" "$base" | tr '.:' '-'
}

# --- worktree enumeration / resolution ---------------------------------------

# Emit "path<TAB>branch" (branch without refs/heads/) for every worktree EXCEPT
# the main one. Detached worktrees emit an empty branch field. Handles the
# porcelain stream's lack of a trailing blank line by flushing at EOF.
# Args: $1 = canonical main root.
wt_task_worktrees() {
  local main="$1" wt="" br=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) wt="${line#worktree }"; br="" ;;
      "branch "*)   br="${line#branch }"; br="${br#refs/heads/}" ;;
      "")
        if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
          printf '%s\t%s\n' "$wt" "$br"
        fi
        wt=""; br=""
        ;;
    esac
  done < <(git worktree list --porcelain 2>/dev/null)
  if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
    printf '%s\t%s\n' "$wt" "$br"
  fi
}

# Pretty-print the task worktrees to stderr (path + branch), for error recovery.
# Args: $1 = canonical main root.
wt_print_worktrees() {
  local main="$1" any="" wt br
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    any=1
    printf '    %-50s %s\n' "$wt" "${br:-(detached)}" >&2
  done < <(wt_task_worktrees "$main")
  [ -n "$any" ] || printf '    (none)\n' >&2
}

# Resolve a user-supplied target to exactly one task-worktree path.
# Matches a target against each worktree by, in order of intent:
#   - canonical path equality (target is/locates a worktree dir)
#   - directory basename, or its tag (basename with the "<repo>-" prefix stripped)
#   - the slugified target vs that tag (so raw "Refactor_Sync" finds "refactor-sync")
#   - the full branch name, or the branch's trailing slug
#   - the leading issue number of the branch slug (so "42" finds feature/42-foo)
# Prints the single match on stdout and returns 0. On zero or multiple matches it
# returns 1 — the caller is expected to list candidates and exit.
# Args: $1 = target, $2 = canonical main root.
wt_resolve() {
  local target="$1" main="$2"
  local tslug repo trp wt br base tag bslug bnum
  tslug="$(wt_slugify "$target")"
  repo="$(basename "$main")"
  trp="$(wt_realpath "$target")"

  local matches=() seen=""
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    base="$(basename "$wt")"
    tag="${base#"${repo}-"}"
    bslug="${br##*/}"
    bnum="${bslug%%-*}"
    if { [ -n "$trp" ] && [ "$trp" = "$(wt_realpath "$wt")" ]; } \
       || [ "$target" = "$base" ] \
       || [ "$target" = "$tag" ] || [ "$tslug" = "$tag" ] \
       || { [ -n "$br" ] && [ "$target" = "$br" ]; } \
       || { [ -n "$bslug" ] && { [ "$target" = "$bslug" ] || [ "$tslug" = "$bslug" ]; }; } \
       || { [ -n "$bnum" ] && [ "$bnum" != "$bslug" ] && [ "$target" = "$bnum" ]; }; then
      case "$seen" in
        *"|$wt|"*) ;;            # already collected
        *) matches+=("$wt"); seen="${seen}|$wt|" ;;
      esac
    fi
  done < <(wt_task_worktrees "$main")

  if [ "${#matches[@]}" -eq 1 ]; then
    printf '%s\n' "${matches[0]}"
    return 0
  fi
  return 1
}

# --- daemon source-hash stamp (issue #190) -----------------------------------
# The reusable "source-hash stamp" primitive: a content hash (sha256) over a long-
# running daemon's source bundle, so the process can detect it is executing code a
# land has since rewritten on disk. Hashing file CONTENT — not mtime — is load-
# bearing: a no-op land that rewrites a file with identical bytes must NOT read as
# changed (no flap-recycle), and a per-worktree checkout that only bumps mtimes must
# not either. A missing bundle path contributes nothing, so an unresolved sibling
# lib never blows up the stamp. '' only when no hasher is available (callers then
# skip the staleness decision). Args: the bundle files, in any order. Consumed by
# hub-otel-watch's daemon self-recycle; overridable in tests.
wt_source_hash() {
  local f
  { for f in "$@"; do [ -f "$f" ] && cat "$f"; done; } | wt_sha256_stdin
}

# sha256 of stdin -> the bare hex digest, portably (shasum on BSD/macOS, sha256sum
# on GNU). '' when neither is available. The single hash pipeline shared by the
# source-hash stamp above and the collector config-version stamp below, so the two
# staleness/recycle checks can never drift apart on a future hasher change.
wt_sha256_stdin() {
  { shasum -a 256 2>/dev/null || sha256sum 2>/dev/null; } | awk '{print $1}'
}

# --- native-OTel message-bridge preflight (auto-populate) --------------------
# A spoke that opted into native OTel (AI_TOOLKIT_OTEL=1) needs the Langfuse
# message bridge (scripts/telemetry/langfuse_message_bridge.py, port :4319) up, or
# the audit events (#93) and LLM request/response I/O the otelcol forks to it never
# reach Langfuse. These helpers bring it up idempotently at spawn so the operator
# runs no manual step; they are best-effort and never fail the spawn.

# True when something is LISTENing on the given localhost TCP port. Split out so
# the preflight decision is unit-testable by overriding it (no live socket). Uses
# lsof when present, else nc; when neither exists, reports "down" so the caller
# attempts a start (a duplicate would simply fail to bind — never two servers).
wt_port_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z localhost "$port" >/dev/null 2>&1
  else
    return 1
  fi
}

# Start the message bridge in the background, detached, logging to a temp file.
# Split from the preflight so the decision logic can be tested without spawning a
# real server. PYTHONPATH=scripts lets the script import its sibling telemetry
# package; LANGFUSE_HOST defaults to the local Langfuse. The child reads its
# REQUIRED config from the environment — LANGFUSE_BASIC_AUTH (KeyErrors without it)
# and BRIDGE_PORT — so re-export them inside a subshell: a non-exported operator
# value still reaches the child (it passed the preflight as a shell-internal read),
# and the credential never lands on the python argv. Args: $1 = repo root.
wt_bridge_launch() {
  local repo_root="$1" log
  log="$(mktemp -t lf-bridge.XXXXXX 2>/dev/null)" || log="/tmp/lf-bridge.log"
  (
    export PYTHONPATH="$repo_root/scripts"
    # env -> config default (LANGFUSE_HOST_DEFAULT, set by wt_resolve_telemetry_config
    # in the launching shell) -> hardcoded local Langfuse (issue #228). So the live
    # bridge forwards to the SAME Langfuse the config names, not a hardcoded localhost.
    export LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_HOST_DEFAULT:-http://localhost:3000}}"
    export LANGFUSE_BASIC_AUTH BRIDGE_PORT
    nohup python3 "$repo_root/scripts/telemetry/langfuse_message_bridge.py" \
      >"$log" 2>&1 &
  )
  echo "→ started Langfuse message bridge on :${BRIDGE_PORT:-4319} (log: $log)"
}

# --- locale-hardened process probes (issue #189) ------------------------------
# The host dev locale is non-C; a bare `pgrep -f` dies "illegal byte sequence" on
# non-ASCII argv (a live process then reads as "not running") and self-matches a
# monitor loop's own argv, while `ps -o lstart=` emits a locale-formatted date the
# parser cannot read (staleness never fires). wt_pgrep / wt_ps_start_epoch are the
# one sanctioned place the raw tools are called; every other control-plane script
# goes through them (locked by tests/unit/test_process_probe_lint.py).

# wt_pgrep <pgrep args...> -> locale-hardened, self-excluding `pgrep`. Runs pgrep
# under LC_ALL=C so non-ASCII argv neither crashes it nor false-negatives, and
# drops the caller's own pids from the result so a loop grepping its own keyword
# (the classic `pgrep -f pytest` self-match) never reports itself: both the
# sourcing shell ($$) and — when invoked as `$(wt_pgrep …)`, where `pgrep -f` on
# Linux matches the forked subshell's inherited argv — that command-substitution
# subshell ($BASHPID; empty on bash 3.2, where it collapses to $$). pgrep's own
# output is captured through a tempfile redirect, NOT a nested `out="$(pgrep …)"`:
# that inner command substitution forks one more short-lived bash that inherits the
# caller's argv, and on Linux `pgrep -f` catches it before it exec's pgrep — a third
# self-match neither $$ nor $BASHPID covers. A plain redirect exec's pgrep straight
# away, so no extra token-bearing shell exists to match. Prints the matching pids,
# one per line. The exit code carries the outcome callers must tell apart — a probe
# failure is never mistaken for "not running":
#   0  one or more OTHER processes match
#   1  nothing matches            -> "not running"
#   2  the probe itself failed    -> "unknown", never conflate with not-running
wt_pgrep() {
  local out rc self=$$ sub="${BASHPID:-$$}" tmp
  tmp="$(mktemp 2>/dev/null)" || return 2
  LC_ALL=C pgrep "$@" >"$tmp" 2>/dev/null
  rc=$?
  [ "$rc" -gt 1 ] && { rm -f "$tmp"; return 2; }   # pgrep 2/3 (syntax/fatal), or a locale death
  out="$(grep -vxF -e "$self" -e "$sub" "$tmp")"
  rm -f "$tmp"
  [ -n "$out" ] || return 1       # empty, or only the caller itself matched
  printf '%s\n' "$out"
}

# PID LISTENing on the bridge port (default :4319), via `lsof -t` — NOT `pgrep -f`,
# which false-negatives on non-ASCII argv under a non-UTF8 locale and would report a
# live bridge as down. '' when nothing listens or lsof is unavailable. Split out so
# the staleness decision is overridable in tests with no real socket probe.
wt_bridge_pid() {
  local port="${1:-4319}"
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1
}

# wt_ps_start_epoch <pid> -> epoch seconds at which the pid started, on stdout.
# Reads `ps -o lstart=` and converts with the portable BSD-then-GNU `date` pattern
# (mirroring wt_epoch_at). Both ps and date run under LC_ALL=C: `ps lstart` is
# locale-formatted (e.g. fr_FR emits "lun. 29 juin"), which `date -f "%a %b %e %T
# %Y"` cannot parse — the same locale trap the issue flags for pgrep, which would
# silently strand the epoch empty and stop staleness from ever firing. The exit
# code separates the two failure modes so a caller never mistakes one for the other:
#   0  epoch printed
#   1  no such process        -> "not running" (empty stdout)
#   2  a start time was read but could not be parsed -> "probe failed"
# Split out so the staleness decision is overridable in tests with no real process.
wt_ps_start_epoch() {
  local pid="$1" lstart epoch
  lstart="$(LC_ALL=C ps -p "$pid" -o lstart= 2>/dev/null)" || return 1  # no such pid
  lstart="${lstart#"${lstart%%[![:space:]]*}"}"   # strip leading padding
  lstart="${lstart%"${lstart##*[![:space:]]}"}"   # strip trailing padding
  [ -n "$lstart" ] || return 1                    # empty ps line -> not running
  epoch="$(LC_ALL=C date -j -f "%a %b %e %T %Y" "$lstart" +%s 2>/dev/null \
    || LC_ALL=C date -d "$lstart" +%s 2>/dev/null)"
  [ -n "$epoch" ] || return 2                     # got a start time, cannot parse
  printf '%s' "$epoch"
}

# Newest mtime (epoch seconds) among the bridge's source bundle: the bridge itself
# plus its only telemetry sibling import, langfuse_audit_events. Reading from the
# MAIN checkout (the preflight's repo_root) makes mtime a reliable change signal —
# a land rewrites the touched files, an untouched land leaves them old — so it does
# not over-fire the way a per-worktree checkout's fresh mtimes would. Portable stat,
# GNU-first (GNU -c %Y, then BSD -f %m): the order is load-bearing, because on GNU
# stat `-f` means "filesystem status" — `stat -f %m FILE` prints a multi-line fs
# block for FILE (taking %m as a missing operand) and exits nonzero, so a BSD-first
# fallback APPENDS the real epoch to that captured garbage and the helper silently
# yielded 0 on Linux (#132). GNU-first fails cleanly on BSD (usage error, empty
# stdout). 0 when none found. Overridable in tests.
# UPGRADE: extend this list if the bridge grows new telemetry.* imports.
wt_bridge_source_mtime() {
  local repo_root="$1" newest=0 f m
  for f in "$repo_root/scripts/telemetry/langfuse_message_bridge.py" \
           "$repo_root/scripts/telemetry/langfuse_audit_events.py"; do
    [ -f "$f" ] || continue
    m="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)" || continue
    [ "$m" -gt "$newest" ] && newest="$m"
  done
  printf '%s' "$newest"
}

# Stop the running bridge (best-effort). Split out so the staleness decision is
# overridable in tests with no real signal sent. Args: $1 = pid.
wt_bridge_kill() { kill "$1" 2>/dev/null || true; }

# Recycle the running bridge IFF its source bundle was modified after the process
# started (a land rewrote the bridge code). Best-effort and idempotent: no pid, no
# resolvable start time, or source not strictly newer leaves the process untouched,
# so the restart fires only on a PROVEN change and never loops. A stale process with
# LANGFUSE_BASIC_AUTH unset is also left running (warn instead of killing a working
# bridge for an un-authable one). The `-gt` is strict at second granularity, so a
# land landing in the very second the bridge (re)started is not seen as stale —
# erring toward no over-fire, an acceptable miss given lands are minutes apart.
# Args: $1 = repo root, $2 = bridge port.
wt_bridge_restart_if_stale() {
  local repo_root="$1" port="$2" pid start src
  pid="$(wt_bridge_pid "$port")"
  [ -n "$pid" ] || return 0
  # `|| true`: wt_ps_start_epoch now returns non-zero on a dead/unparseable pid, and
  # this runs under worktree-new.sh's `set -e`, where a failing command substitution
  # in an assignment aborts before the guard below. Swallow it so the preflight stays
  # best-effort (never fails the spawn); the empty-start guard covers both failures.
  start="$(wt_ps_start_epoch "$pid")" || true
  [ -n "$start" ] || return 0
  src="$(wt_bridge_source_mtime "$repo_root")"
  [ "$src" -gt "$start" ] 2>/dev/null || return 0
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "Langfuse bridge source changed but LANGFUSE_BASIC_AUTH unset — leaving the running (stale) bridge; restart manually after exporting auth"
    return 0
  fi
  wt_bridge_kill "$pid"
  wt_bridge_launch "$repo_root"
}

# Idempotently ensure the message bridge is up AND current for an opted-in spoke. A
# no-op unless AI_TOOLKIT_OTEL=1. When :4319 already listens, delegate to
# wt_bridge_restart_if_stale — which recycles the process only when its source
# bundle proves it is running stale code, and otherwise leaves it untouched (no
# second bridge, no needless churn). When down: never starts a second bridge. When
# LANGFUSE_BASIC_AUTH is unset the bridge can't authenticate to Langfuse, so warn
# (audit events + LLM I/O won't land) but DO NOT fail the spawn. Args: $1 = repo root.
wt_otel_bridge_preflight() {
  local repo_root="$1" port="${BRIDGE_PORT:-4319}"
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  if wt_port_listening "$port"; then
    wt_bridge_restart_if_stale "$repo_root" "$port"
    return 0
  fi
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel bridge down on :$port and LANGFUSE_BASIC_AUTH unset — audit events (#93) + LLM I/O won't reach Langfuse; spoke still launches"
    return 0
  fi
  wt_bridge_launch "$repo_root"
}

# --- native-OTel collector preflight (auto-ensure) ---------------------------
# A spoke that opted into native OTel (AI_TOOLKIT_OTEL=1) exports its
# traces/logs/metrics to the otelcol collector on :4317; the collector in turn
# forks the LLM I/O + audit events to the message bridge (:4319). If the
# collector is down (fresh boot, crashed container, never started) every spoke
# exports into a dead port and nothing reaches Langfuse. This helper brings the
# collector up idempotently at spawn — mirroring wt_otel_bridge_preflight — so
# the operator runs no manual step. Best-effort: it never fails the spawn.

# The collector image and published-port set, as single sources of truth shared
# by the launch and by the staleness signature below — so a future bump to either
# flows into the config-version label and a container stamped with the old value
# is detected stale. The port flags are intentionally word-split at the `docker
# run` call site.
WT_COLLECTOR_IMAGE="otel/opentelemetry-collector-contrib:latest"
WT_COLLECTOR_PORT_FLAGS="-p 4317:4317 -p 4318:4318 -p 4418:4418 -p 8889:8889"

# Combined staleness signature for the collector: a hash over the otelcol.yaml
# CONTENT plus the expected port set + image. A content hash (not the file's
# mtime) is the right signal because the config is bind-mounted — the running
# container's mounted file already equals on-disk, and a per-worktree checkout
# rewrites mtimes without changing content. Any real change to config, ports, or
# image bumps the signature. Empty when the config is missing (caller then leaves
# the running instance untouched). Split out so it is overridable in tests.
# Args: $1 = repo root (holds dashboard/langfuse/otelcol.yaml).
wt_collector_config_version() {
  local cfg="$1/dashboard/langfuse/otelcol.yaml"
  [ -f "$cfg" ] || return 0
  { cat "$cfg"; printf '%s\n%s\n' "$WT_COLLECTOR_PORT_FLAGS" "$WT_COLLECTOR_IMAGE"; } \
    | wt_sha256_stdin
}

# Start the otelcol collector (lf-collector) in a detached Docker container.
# Split from the preflight so the decision logic stays unit-testable (override
# wt_port_listening, no real `docker run`). The non-secret connection endpoints
# default to the local stack when the operator left them unset; the `docker -e
# VAR` (valueless) form forwards them — and LANGFUSE_BASIC_AUTH — from this
# shell, so re-export them in a subshell. LANGFUSE_BASIC_AUTH is forwarded
# VERBATIM: wrapping it in extra quotes makes the collector's Authorization
# header 401 while metrics still flow (looks like a pipeline bug but is auth).
# Stamps the config-version label so a later spawn can detect a stale container.
# Args: $1 = repo root (holds dashboard/langfuse/otelcol.yaml).
wt_collector_launch() {
  local repo_root="$1" version
  version="$(wt_collector_config_version "$repo_root")"
  (
    export LANGFUSE_OTLP_ENDPOINT="${LANGFUSE_OTLP_ENDPOINT:-http://host.docker.internal:3000/api/public/otel}"
    export BRIDGE_OTLP_ENDPOINT="${BRIDGE_OTLP_ENDPOINT:-http://host.docker.internal:4319}"
    export LANGFUSE_BASIC_AUTH
    # shellcheck disable=SC2086  # WT_COLLECTOR_PORT_FLAGS is meant to word-split.
    docker run -d --name lf-collector --add-host=host.docker.internal:host-gateway \
      --label "ai-toolkit.config-version=$version" \
      $WT_COLLECTOR_PORT_FLAGS \
      -e LANGFUSE_OTLP_ENDPOINT -e LANGFUSE_BASIC_AUTH -e BRIDGE_OTLP_ENDPOINT \
      -v "$repo_root/dashboard/langfuse/otelcol.yaml:/etc/otelcol-contrib/config.yaml" \
      "$WT_COLLECTOR_IMAGE" >/dev/null 2>&1
  )
  echo "→ started lf-collector (otelcol) on :4317/:4318/:4418/:8889"
}

# The config-version label of the running lf-collector, or '' on any failure (no
# such container, unlabeled pre-feature container, docker unreachable). Split out
# so the staleness decision is overridable in tests with no real `docker inspect`.
wt_collector_running_version() {
  docker inspect -f '{{ index .Config.Labels "ai-toolkit.config-version" }}' \
    lf-collector 2>/dev/null || true
}

# Tear down the running collector (best-effort). Split out so the staleness
# decision is overridable in tests with no real `docker rm`.
wt_collector_remove() {
  docker rm -f lf-collector >/dev/null 2>&1 || true
}

# The lifecycle status of the lf-collector container — e.g. running/exited/created/
# dead (also restarting/paused/removing) — or '' when the container is absent or
# docker is unreachable. The list is illustrative, not exhaustive: the caller
# treats ANY non-running state as recoverable. Split out so the recover-when-dead
# decision is overridable in tests with no real `docker inspect`.
wt_collector_container_status() {
  docker inspect -f '{{ .State.Status }}' lf-collector 2>/dev/null || true
}

# Recover a stopped lf-collector so a subsequent launch's --name can't clash. The
# spawn preflight starts the collector only when :4317 is DOWN, but an
# Exited/Created/Dead container still owns the `lf-collector` name — a bare `docker
# run --name lf-collector` then fails the name check (swallowed best-effort) and
# never recovers: start-if-absent, not restart-if-dead (#115). So when a
# non-running container exists, tear it down here; the caller relaunches a fresh
# one. Absent (or docker unreachable → '') means nothing to recover. A container
# reporting `running` is left untouched ON PURPOSE — never tear down a possibly
# healthy or still-starting collector. The running guard is NOT dead code: the
# down path can be entered with a running container (a startup race before :4317
# binds, a bind to the wrong interface), and in that corner recovery is
# deliberately skipped. Split out so the decision is unit-testable with docker
# overridden.
# UPGRADE: a running-but-wedged collector (up, not serving :4317) is not
# auto-healed — out of #115's Exited/Created/Dead scope; add a liveness probe if it
# recurs.
wt_collector_recover_dead() {
  local status
  status="$(wt_collector_container_status)"
  [ -n "$status" ] || return 0
  [ "$status" = "running" ] && return 0
  wt_collector_remove
}

# Recycle the running collector IFF its stamped config-version differs from the
# current one (an otelcol.yaml / port / image change landed). Best-effort and
# idempotent: an unhashable config or a missing/unreadable label leaves the
# instance untouched, so the restart fires only on a PROVEN change and never
# loops. A stale instance with LANGFUSE_BASIC_AUTH unset is also left running
# (warn instead of tearing a working instance down for an un-authable one).
# Args: $1 = repo root.
wt_collector_restart_if_stale() {
  local repo_root="$1" cur run
  cur="$(wt_collector_config_version "$repo_root")"
  [ -n "$cur" ] || return 0
  run="$(wt_collector_running_version)"
  [ -n "$run" ] || return 0
  [ "$run" != "$cur" ] || return 0
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel collector config changed but LANGFUSE_BASIC_AUTH unset — leaving the running (stale) lf-collector; restart manually after exporting auth"
    return 0
  fi
  wt_collector_remove
  wt_collector_launch "$repo_root"
}

# Idempotently ensure the otelcol collector is up AND current for an opted-in
# spoke. A no-op unless AI_TOOLKIT_OTEL=1 (AI_TOOLKIT_OTEL=0 is a clean full
# opt-out). When :4317 already listens, delegate to wt_collector_restart_if_stale
# — which recycles the container only when its stamped config-version proves it is
# running stale code/config, and otherwise leaves it untouched (no second
# collector, no needless churn). When down: first recover a stopped lf-collector
# (wt_collector_recover_dead removes an Exited/Created/Dead container that would
# otherwise fail a fresh launch's --name check, swallowed best-effort, #115), then
# start exactly one collector. When LANGFUSE_BASIC_AUTH is unset the collector
# can't authenticate to Langfuse, so warn (telemetry won't land) and leave any
# stopped container in place — recovering without an authed relaunch only strands
# the port — but DO NOT fail the spawn (same posture as wt_otel_bridge_preflight).
# Run BEFORE the bridge preflight: the collector forks to the bridge, so both must
# be up before the spoke's first export. Args: $1 = repo root.
wt_otel_collector_preflight() {
  local repo_root="$1" port=4317
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  if wt_port_listening "$port"; then
    wt_collector_restart_if_stale "$repo_root"
    return 0
  fi
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel collector down on :$port and LANGFUSE_BASIC_AUTH unset — telemetry won't reach Langfuse; spoke still launches"
    return 0
  fi
  wt_collector_recover_dead
  wt_collector_launch "$repo_root"
}

# --- watchdog auto-arm (#138) -------------------------------------------------
# The preflights above cover the spawn INSTANT; nothing re-ensures a collector
# that dies mid-run (a machine sleep left lf-collector Exited for ~24 min while
# a live spoke dropped every span at source). Arm the hub-side watchdog at every
# spawn: hub-otel-watch.sh --daemon re-runs the same ensure paths each tick
# while ≥1 spoke pane is live and exits itself when the last pane closes, so
# capture self-heals across sleep/wake with no human in the loop.
#
# Best-effort and idempotent, same posture as the preflights: a no-op unless
# AI_TOOLKIT_OTEL=1, a no-op while a live daemon already holds the pidfile (the
# daemon's own singleton guard stays authoritative — this pre-check only avoids
# forking a doomed child per spawn), a warning (never a spawn failure) when the
# watch script is unresolvable. The subshell exports what the detached child
# needs: the OTel opt-in and Langfuse auth are plain (unexported) assignments in
# worktree-new.sh, and MAIN_ROOT pins the daemon's ensure target to this hub.
# Args: $1 = repo root.
wt_otel_watch_arm() {
  local repo_root="$1" bin="" pidfile pid cand common
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  for cand in \
    "${HUB_OTEL_WATCH_BIN:-}" \
    "$repo_root/shared/skills/hub/scripts/hub-otel-watch.sh" \
    "$repo_root/.ai-toolkit/scripts/hub-otel-watch.sh" \
    "$repo_root/.claude/skills/hub/scripts/hub-otel-watch.sh"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then bin="$cand"; break; fi
  done
  if [ -z "$bin" ]; then
    wt_warn "hub-otel-watch.sh not found under $repo_root — watchdog not armed; a mid-run collector death won't self-heal (run hub-otel-watch.sh --daemon manually)"
    return 0
  fi
  pidfile="${HUB_OTEL_WATCH_PIDFILE:-}"
  if [ -z "$pidfile" ]; then
    common="$(git -C "$repo_root" rev-parse --git-common-dir 2>/dev/null)" || common=""
    case "$common" in
      "" | /*) ;;
      *) common="$repo_root/$common" ;;
    esac
    [ -n "$common" ] && pidfile="$common/hub-otel-watch.pid"
  fi
  if [ -n "$pidfile" ] && [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
  fi
  (
    export AI_TOOLKIT_OTEL LANGFUSE_BASIC_AUTH
    export MAIN_ROOT="$repo_root"
    export HUB_OTEL_WATCH_PIDFILE HUB_OTEL_WATCH_LOG HUB_OTEL_WATCH_INTERVAL HUB_OTEL_WATCH_IDLE_TICKS
    nohup bash "$bin" --daemon >/dev/null 2>&1 &
  )
  echo "→ armed hub-otel-watch daemon (collector/bridge self-heal across sleep/wake)"
}

# --- GitHub lifecycle-label mirror (issue #236) -------------------------------
# Mirror the local spoke lifecycle onto its GitHub issue as labels + a dispatch
# comment, so the issue list shows what local state (worktree branches, git
# gate/ready/blocked tags, .afk-state) otherwise hides: dispatched, parked on a
# gate, ready, blocked, mode, lane. GitHub is a READ-ONLY mirror of the local
# markers — every write here is BEST-EFFORT and TIME-BOUNDED, so a failed / hung /
# absent / opted-out `gh` never fails a dispatch, a land, or a drain tick (the
# offline-safe local markers stay the source of truth).
#
# Single-writer contract (issue #236): dispatch (worktree-new.sh) stamps
# status:in-progress + mode:* + lane:spoke; the hub-ready-watch → hub-notify watch
# loop flips status:* on gate/ready/blocked marker transitions; hub-afk stamps
# status:blocked on a supervisor escalation; worktree-land clears them all at the
# issue-close step. spoke-ready.sh deliberately writes NO labels — the hub mirrors.
#
# Only issue-backed spokes mirror: express/quick/micro lanes carry no issue by
# construction, so callers gate the whole thing on a numeric issue id.

# The label taxonomy, as single sources of truth. status:* is mutually exclusive
# (a set swaps the sibling out); mode:*/lane:* ride alongside.
WT_GH_STATUS_LABELS="status:in-progress status:gate status:ready status:blocked"
WT_GH_MODE_LABELS="mode:afk mode:attended"
WT_GH_LANE_LABELS="lane:spoke"

# wt_gh_lifecycle_enabled — the mirror is ON by default; AI_TOOLKIT_GH_LIFECYCLE_LABELS=0
# is a clean full opt-out (parallel to #223's opt-IN afk:* scheduling labels, which
# convey a different thing). Any other value (incl. unset) stays on.
wt_gh_lifecycle_enabled() { [ "${AI_TOOLKIT_GH_LIFECYCLE_LABELS:-1}" != "0" ]; }

# _wt_gh_timeout_bin -> the installed coreutils timeout binary (timeout | gtimeout),
# or empty when neither is present (the default macOS hub ships neither — see the
# portable fallback in wt_gh).
_wt_gh_timeout_bin() {
  if command -v timeout >/dev/null 2>&1; then printf 'timeout\n'
  elif command -v gtimeout >/dev/null 2>&1; then printf 'gtimeout\n'
  fi
}

# _wt_kill_tree <pid> <signal> — signal a pid and all its descendants leaf-first, so a
# bounded gh's children (a forked git/curl helper) die with it rather than being orphaned
# holding the pipe open. Mirrors hub-afk's _afk_kill_tree but self-contained here so the
# lib carries no hub dependency; wt_pgrep -P matches by numeric parent pid under LC_ALL=C
# (ASCII-safe), so the non-ASCII `pgrep -f` hazard (#189) doesn't apply.
_wt_kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(wt_pgrep -P "$pid" 2>/dev/null); do _wt_kill_tree "$child" "$sig"; done
  kill "-$sig" "$pid" 2>/dev/null || true
}

# _wt_gh_run <gh args...> — bounded gh returning gh's REAL exit code (0 success; nonzero
# on a gh failure OR a killed timeout; 127 when gh is absent). gh is ALWAYS time-bounded
# (AI_TOOLKIT_GH_TIMEOUT seconds, default 10): under the coreutils timeout when installed,
# else a self-contained portable fallback that backgrounds gh and kill-trees it past the
# deadline (SIGTERM, then SIGKILL after a short grace) — so a HUNG gh (a black-hole network,
# not clean-offline) can NEVER freeze a caller on a coreutils-less host (#170 guarantee).
# Every branch uses an `if`/`else` (never `cmd; return $?`) so capturing the rc can't itself
# trip a set -e caller's errexit. The seeder needs the real rc to tell a real create from a
# swallowed failure; callers that don't care use wt_gh (which discards it).
_wt_gh_run() {
  command -v gh >/dev/null 2>&1 || return 127
  local budget="${AI_TOOLKIT_GH_TIMEOUT:-10}"
  case "$budget" in '' | *[!0-9]*) budget=10 ;; esac
  local tbin; tbin="$(_wt_gh_timeout_bin)"
  if [ -n "$tbin" ]; then
    if "$tbin" "$budget" gh "$@" >/dev/null 2>&1; then return 0; else return $?; fi
  fi
  # Portable fallback: background gh + a detached killer that kill-trees it after the
  # budget. When gh finishes first the killer is cancelled immediately (no lingering
  # sleep), so the fast path stays fast.
  local grace="${AI_TOOLKIT_GH_KILL_AFTER:-2}"
  case "$grace" in '' | *[!0-9]*) grace=2 ;; esac
  local cmd_pid killer rc
  gh "$@" >/dev/null 2>&1 &
  cmd_pid=$!
  ( sleep "$budget"; _wt_kill_tree "$cmd_pid" TERM; sleep "$grace"; _wt_kill_tree "$cmd_pid" KILL ) \
    </dev/null >/dev/null 2>&1 &
  killer=$!
  if wait "$cmd_pid" 2>/dev/null; then rc=0; else rc=$?; fi
  _wt_kill_tree "$killer" TERM 2>/dev/null || true   # gh finished — cancel the pending killer
  wait "$killer" 2>/dev/null || true
  return "$rc"
}

# wt_gh <gh args...> — one BEST-EFFORT, time-bounded gh invocation. A no-op (rc 0) when
# the mirror is disabled or gh is absent; otherwise runs _wt_gh_run and DISCARDS its exit
# code. ALWAYS returns 0 — a gh failure or a killed hang must never abort a set -e caller
# mid-dispatch/land/tick. Used for the label edits and the dispatch comment, where the
# outcome doesn't gate anything.
wt_gh() {
  wt_gh_lifecycle_enabled || return 0
  _wt_gh_run "$@" || true
  return 0
}

# wt_gh_ensure_label <name> <color> <desc> — idempotently create/update a label
# (`gh label create --force` updates an existing one rather than erroring), so a
# later --add-label/--remove-label of it can never fail the whole edit for a missing
# repo label. RETURNS the real gh exit code (via _wt_gh_run) so the seeder can gate the
# persistent marker on a proven success. Call it in an `&&`/`||`/`if` context under set -e.
wt_gh_ensure_label() {
  _wt_gh_run label create "$1" --color "$2" --description "$3" --force
}

# _wt_gh_seed_dir -> the dir holding the once-per-repo seed marker. WT_GH_SEED_DIR
# overrides it (tests / a caller with no git dir); otherwise the git common dir
# (shared across a repo's worktrees, so ONE dispatch per repo seeds and every later
# dispatch/transition skips the label-create round-trips). Empty (rc 1) when neither
# resolves — the seeder then falls back to a per-process guard.
_wt_gh_seed_dir() {
  if [ -n "${WT_GH_SEED_DIR:-}" ]; then printf '%s' "$WT_GH_SEED_DIR"; return 0; fi
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  case "$common" in /*) ;; *) common="$PWD/$common" ;; esac
  printf '%s' "$common"
}

# _wt_gh_seed_labels — ensure ALL status:*/mode:*/lane:* labels exist in the repo, so a
# later --add-label/--remove-label of any of them can never fail the whole edit for a
# missing repo label. Idempotent and cheap-once: a PERSISTENT once-per-repo marker under
# _wt_gh_seed_dir means only the FIRST *successful* dispatch/transition per repo pays the
# label-create round-trips (the review flagged the per-process guard re-seeding every
# dispatch). Falls back to a per-process shell guard when no seed dir resolves. All three
# label writers (set_status / apply_dispatch / clear) route through this, so a status-only
# transition still guarantees the mode/lane labels exist too.
#
# The marker is persisted ONLY when EVERY create succeeded (all_ok). A first seed whose gh
# calls fail — offline, unauthed, or a hung gh the timeout kills (the exact black-hole path
# #236 hardens) — must NOT stamp the marker, or it would permanently skip re-seeding and
# leave the mirror dead for the repo with no self-heal. On any failure the marker stays
# unwritten and the NEXT transition retries seeding (gh label create --force is idempotent,
# so a re-seed after a partial success is harmless). Recovery from a stale marker is simply
# `rm <git-common-dir>/.gh-lifecycle-labels-seeded`.
_WT_GH_LABELS_SEEDED=""
_wt_gh_seed_labels() {
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  local dir marker all_ok=1
  dir="$(_wt_gh_seed_dir 2>/dev/null || true)"
  if [ -n "$dir" ]; then
    marker="$dir/.gh-lifecycle-labels-seeded"
    [ -f "$marker" ] && return 0
  else
    [ -n "$_WT_GH_LABELS_SEEDED" ] && return 0
  fi
  # `|| all_ok=0` keeps errexit from aborting on a failed create (the failure is on the
  # left of ||), and records that this seed pass is not fully proven.
  wt_gh_ensure_label "status:in-progress" "1d76db" "spoke dispatched, working" || all_ok=0
  wt_gh_ensure_label "status:gate"        "fbca04" "parked on a plan gate" || all_ok=0
  wt_gh_ensure_label "status:ready"       "0e8a16" "final push, awaiting land" || all_ok=0
  wt_gh_ensure_label "status:blocked"     "b60205" "escalated, needs a human" || all_ok=0
  wt_gh_ensure_label "mode:afk"      "5319e7" "unattended /afk drain spoke" || all_ok=0
  wt_gh_ensure_label "mode:attended" "c5def5" "attended (interactive) spoke" || all_ok=0
  wt_gh_ensure_label "lane:spoke"    "bfdadc" "issue-backed full-cycle spoke" || all_ok=0
  [ "$all_ok" = "1" ] || return 0   # a failed seed leaves NO marker so it self-heals
  if [ -n "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null || true
    : > "$marker" 2>/dev/null || true
  else
    _WT_GH_LABELS_SEEDED=1
  fi
}

# wt_gh_set_status_label <issue> <status-label> — swap the issue's status:* label
# to <status-label> (e.g. status:gate), removing the other three. mode:*/lane:*
# are left intact (a gate/ready/blocked transition changes only the status). Used
# by the hub-notify watch loop and hub-afk's blocked escalation.
wt_gh_set_status_label() {
  local issue="$1" want="$2" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  args+=(--add-label "$want")
  for s in $WT_GH_STATUS_LABELS; do
    [ "$s" = "$want" ] && continue
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_apply_dispatch_labels <issue> <mode> <lane> — stamp a freshly-dispatched
# spoke: status:in-progress + mode:<mode> + lane:<lane>, swapping out any stale
# status sibling or other-mode label a reused issue number carried. Best-effort.
wt_gh_apply_dispatch_labels() {
  local issue="$1" mode="$2" lane="$3" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  args+=(--add-label "status:in-progress" --add-label "mode:$mode" --add-label "lane:$lane")
  for s in $WT_GH_STATUS_LABELS; do
    [ "$s" = "status:in-progress" ] && continue
    args+=(--remove-label "$s")
  done
  for s in $WT_GH_MODE_LABELS; do
    [ "$s" = "mode:$mode" ] && continue
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_clear_lifecycle_labels <issue> — remove every status:*/mode:*/lane:* label
# from the issue (a landed/torn-down spoke no longer has live state). The close
# comment worktree-land writes is separate and unchanged. Best-effort.
wt_gh_clear_lifecycle_labels() {
  local issue="$1" args=() s
  wt_gh_lifecycle_enabled || return 0
  command -v gh >/dev/null 2>&1 || return 0
  _wt_gh_seed_labels
  for s in $WT_GH_STATUS_LABELS $WT_GH_MODE_LABELS $WT_GH_LANE_LABELS; do
    args+=(--remove-label "$s")
  done
  wt_gh issue edit "$issue" "${args[@]}"
}

# wt_gh_dispatch_comment <issue> <body> — post the one-time dispatch comment
# linking the issue to its live spoke (branch, worktree, tmux window, spoke_run_id).
# Best-effort.
wt_gh_dispatch_comment() {
  wt_gh issue comment "$1" --body "$2"
}

# --- #300 lifecycle transition log ------------------------------------------
# The four lifecycle ACTORS (worktree-new, spoke-ready, spoke-push,
# worktree-land) all source this lib, so the log's locator + a guarded wrapper
# live here ONCE rather than in each. Dual layout, mirroring the telemetry.sh
# block above: a synced target has transition-log.sh as our sibling in
# .ai-toolkit/scripts/; the source tree has it at shared/skills/hub/scripts/.
# Best-effort by contract (#300): if the lib is absent the wrappers are no-ops,
# so no actor ever fails because the log could not be written.
for _c in "$_WT_LIB_DIR/transition-log.sh" \
          "$_WT_LIB_DIR/../shared/skills/hub/scripts/transition-log.sh"; do
  if [ -f "$_c" ]; then
    # shellcheck source=/dev/null
    . "$_c" 2>/dev/null || true
    break
  fi
done
unset _c

# wt_tlog_transition <issue> <to> <actor> <cause> [evidence-json] [episode]
# wt_tlog_event      <issue> <event> <actor> [lane] [episode] [evidence-json]
# Guarded fronts for the log: silently no-op when the lib is unavailable or the
# issue is not numeric (an ad-hoc /quick slug has no issue to key a log by).
wt_tlog_transition() {
  case "${1:-}" in '' | *[!0-9]*) return 0 ;; esac
  command -v afk_tlog_transition >/dev/null 2>&1 || return 0
  afk_tlog_transition "$@" || true
}

wt_tlog_event() {
  case "${1:-}" in '' | *[!0-9]*) return 0 ;; esac
  command -v afk_tlog_event >/dev/null 2>&1 || return 0
  afk_tlog_event "$@" || true
}

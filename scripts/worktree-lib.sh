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

# Resolve the repo name for the cross-project telemetry dimension (issue #343), mirroring
# the land-time repo:<name> trace tag (#231): the origin remote's basename (a trailing .git
# stripped), else the checkout dir basename. Best-effort — an unresolvable name prints empty
# and the caller omits the repo attribute.
# Usage: wt_repo_name <dir>
wt_repo_name() {
  local dir="$1" name
  name="$(git -C "$dir" remote get-url origin 2>/dev/null | sed -E 's#.*[/:]##; s#\.git$##')"
  [ -n "$name" ] || name="$(basename "$dir")"
  printf '%s' "$name"
}

wt_native_otel_prefix() {
  local spoke_run_id="$1" body_dir="$2" repo="${3:-}"
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  # Default the non-secret endpoints when unset (operator override preserved); the
  # normal gRPC :4317 / beta HTTP :4418 split is load-bearing — a beta endpoint on the
  # normal host:port silently kills trace+log export. The span sink is OTLP-HTTP :4318.
  : "${OTEL_EXPORTER_OTLP_ENDPOINT:=http://localhost:4317}"
  : "${BETA_TRACING_ENDPOINT:=http://localhost:4418}"
  wt_default_span_endpoint
  # OTEL_RESOURCE_ATTRIBUTES is a comma-separated key=value list. repo=<name> rides
  # alongside spoke_run_id (issue #343) so the collector can stamp a per-repo dimension
  # (trace tag + observation metadata) onto every live span, making one shared Langfuse
  # project sliceable per repo. An empty repo is omitted (never an empty attribute).
  local resource="spoke_run_id=${spoke_run_id}"
  if [ -n "$repo" ]; then
    resource="${resource},repo=${repo}"
  fi
  printf 'CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp ENABLE_BETA_TRACING_DETAILED=1 OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false OTEL_EXPORTER_OTLP_PROTOCOL=grpc OTEL_EXPORTER_OTLP_ENDPOINT=%s BETA_TRACING_ENDPOINT=%s AI_TOOLKIT_OTEL_SPAN_ENDPOINT=%s OTEL_LOG_USER_PROMPTS=1 OTEL_LOG_TOOL_DETAILS=1 OTEL_LOG_TOOL_CONTENT=1 OTEL_LOG_RAW_API_BODIES=%s AI_TOOLKIT_OTEL_BODY_DIR=%s OTEL_RESOURCE_ATTRIBUTES=%s ' \
    "$(printf '%q' "$OTEL_EXPORTER_OTLP_ENDPOINT")" \
    "$(printf '%q' "$BETA_TRACING_ENDPOINT")" \
    "$(printf '%q' "$AI_TOOLKIT_OTEL_SPAN_ENDPOINT")" \
    "$(printf '%q' "file:${body_dir}")" \
    "$(printf '%q' "$body_dir")" \
    "$(printf '%q' "$resource")"
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

# --- native-OTel preflight + gh lifecycle-label mirror (extracted modules) ----
# Issue #353 split this lib behind a thin entry: the native-OTel bridge/collector
# preflight machinery moved to worktree-otel-lib.sh and the gh lifecycle-label
# mirror to worktree-gh-lib.sh, so a change to either stops serializing the drain
# on this file's Scope: token (AFK Design Principle 7). Both are co-located siblings
# (this checkout's scripts/, a synced target's .ai-toolkit/scripts/), so the single
# $_WT_LIB_DIR candidate resolves in both. They carry functions consumers call
# UNCONDITIONALLY (wt_otel_*_preflight, wt_gh_*), so a missing/unreadable module is a
# LOUD failure (AFK Design Principle 2: fail loud, never silently degrade) — not the
# silent skip telemetry.sh/transition-log.sh use for their genuinely-optional libs.
# The [ -r ] guard is load-bearing: `. ` of a missing file is a special builtin that
# can exit the sourcing shell, escaping into every consumer that sources this lib.
for _wt_mod in otel gh; do
  _wt_modfile="$_WT_LIB_DIR/worktree-$_wt_mod-lib.sh"
  if [ -r "$_wt_modfile" ]; then
    # shellcheck source=/dev/null
    . "$_wt_modfile"
  else
    wt_warn "required module worktree-$_wt_mod-lib.sh missing/unreadable at $_WT_LIB_DIR — OTel preflight / gh lifecycle labels unavailable"
  fi
done
unset _wt_mod _wt_modfile

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

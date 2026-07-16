#!/usr/bin/env bash
# sync-to-repo.sh — Generate tool-specific configs from shared/ into a target repo.
# Reads per-directory metadata.yml for frontmatter. Python 3 stdlib only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SHARED_DIR="$REPO_DIR/shared"

# Declarative toolkit config (issue #142): the single source of truth for model
# routing + base_branch. Overridable via AI_TOOLKIT_CONFIG (used by tests).
AI_TOOLKIT_CONFIG="${AI_TOOLKIT_CONFIG:-$REPO_DIR/settings/ai-toolkit.yml}"

GREEN='\033[0;32m'  YELLOW='\033[1;33m'  RED='\033[0;31m'  BLUE='\033[0;34m'  NC='\033[0m'
info()    { echo -e "${GREEN}✓${NC} $1"; }
warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
error()   { echo -e "${RED}✗${NC} $1" >&2; }
section() { echo -e "\n${BLUE}── $1 ──${NC}"; }

usage() {
    echo "Usage: $0 <target-repo-path> [tool] [--with-git-hooks] [--local-only] [--dry-run]"
    echo ""
    echo "Tools: copilot, cursor, claude, all (default: all)"
    echo ""
    echo "Flags:"
    echo "  --with-git-hooks   Also install the cage scripts as NATIVE git hooks"
    echo "                     (fallback enforcement, independent of the agent runtime)"
    echo "  --local-only       Exclude the synced ai-toolkit paths in the target's"
    echo "                     .git/info/exclude (per-clone, never committed), so a"
    echo "                     PERSONAL deployment does not propagate to teammates"
    echo "  --dry-run          Print what would be written/deleted without touching"
    echo "                     the target"
    echo ""
    echo "Examples:"
    echo "  $0 ~/Repos/my-project                       # Sync all tools"
    echo "  $0 ~/Repos/my-project copilot                # Copilot only"
    echo "  $0 ~/Repos/my-project cursor                 # Cursor only"
    echo "  $0 ~/Repos/my-project claude                 # Claude only"
    echo "  $0 ~/Repos/my-project all --with-git-hooks   # Sync + native git hooks"
    echo "  $0 ~/Repos/my-project cursor --dry-run       # Preview only"
    exit 1
}

[ $# -lt 1 ] && usage
TARGET=""
TOOL="all"
WITH_GIT_HOOKS=0
DRY_RUN=0
LOCAL_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --with-git-hooks) WITH_GIT_HOOKS=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --local-only) LOCAL_ONLY=1 ;;
        copilot|cursor|claude|all) TOOL="$arg" ;;
        -*) error "Unknown flag: $arg"; usage ;;
        *)
            if [ -z "$TARGET" ]; then
                TARGET="$arg"
            else
                error "Unexpected argument: $arg"; usage
            fi
            ;;
    esac
done
[ -z "$TARGET" ] && usage
[ ! -d "$TARGET" ] && { error "Target directory does not exist: $TARGET"; exit 1; }
TARGET="$(cd "$TARGET" && pwd)"

# A normal checkout has a .git directory; a linked worktree has a .git FILE
# (gitlink). Accept either so syncing into a worktree is not flagged as non-repo.
if [ ! -e "$TARGET/.git" ]; then
    warn "Target does not appear to be a git repository: $TARGET"
    read -rp "Continue anyway? [y/N] " confirm
    [[ ! "$confirm" =~ ^[Yy]$ ]] && { echo "Aborted."; exit 0; }
fi

# ─── Python helper ───
# Reads metadata.yml, merges shared defaults with per-tool overrides,
# emits only the requested fields.
# Args: $1=metadata.yml  $2=tool  $3=comma-separated fields
# Output: "name<TAB>field1: val\nfield2: val" per item
query_metadata() {
    local meta_file="$1" tool="$2" fields="$3" config="${4:-}"
    [ -f "$meta_file" ] || return 0
    # A 4th arg passes the ai-toolkit config so the parser overlays each agent's
    # model/effort from it (issue #142) — the config, not metadata.yml, routes.
    if [ -n "$config" ] && [ -f "$config" ]; then
        python3 "$SCRIPT_DIR/metadata_parser.py" "$meta_file" "$tool" "$fields" "$config"
    else
        python3 "$SCRIPT_DIR/metadata_parser.py" "$meta_file" "$tool" "$fields"
    fi
}

# ─── File recording for the sync manifest ───
# Sync loops run inside `| while read` pipelines (subshells), so appends to a
# plain bash array would not survive. Recording goes through a temp FILE
# (one per tool, created in sync_tool) which subshells can append to.
RECORD_FILE=""
record_file() {
    # $1 = path relative to $TARGET
    echo "$1" >> "$RECORD_FILE"
}

# ─── Helper: create a directory (no-op in dry-run) ───
make_dir() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    mkdir -p "$@"
}

# ─── Helper: write file with YAML frontmatter ───
add_frontmatter() {
    local src="$1" dst="$2" meta="$3"
    local rel="${dst#"$TARGET/"}"
    record_file "$rel"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would write $rel"
        return 0
    fi
    { echo "---"; echo -e "$meta"; echo "---"; echo ""; cat "$src"; } > "$dst"
}

# ─── Helper: plain file copy (recorded, dry-run aware) ───
copy_file() {
    local src="$1" dst="$2"
    local rel="${dst#"$TARGET/"}"
    record_file "$rel"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would write $rel"
        return 0
    fi
    cp "$src" "$dst"
}

# ─── Helper: was this relpath already written in the current sync? ───
# Used by the plain-copy fallback loops: in real mode the file exists on disk,
# but in dry-run nothing is written, so the record file is the source of truth.
already_synced() {
    grep -qxF "$1" "$RECORD_FILE" 2>/dev/null
}

# ─── Helper: copy skill subdirectories (references, scripts, templates, assets) ───
copy_skill_subdirs() {
    local src_dir="$1" dst_dir="$2"
    local rel_dst="${dst_dir#"$TARGET/"}"
    local subdir f rel
    for subdir in references scripts templates assets; do
        [ -d "$src_dir/$subdir" ] || continue
        if [ "$DRY_RUN" -eq 0 ]; then
            cp -R "$src_dir/$subdir" "$dst_dir/"
            info "  └── $subdir/"
        fi
        # Record each copied file (enumerated from the source tree)
        find "$src_dir/$subdir" -type f | while read -r f; do
            rel="$rel_dst/$subdir/${f#"$src_dir/$subdir/"}"
            record_file "$rel"
            if [ "$DRY_RUN" -eq 1 ]; then
                echo "[dry-run] would write $rel"
            fi
        done
    done
}

# ─── Field sets per tool per category ───
COPILOT_RULE_FIELDS="name,description,applyTo,excludeAgent"
CURSOR_RULE_FIELDS="description,globs,alwaysApply"
CLAUDE_RULE_FIELDS="paths"

COPILOT_SKILL_FIELDS="name,description,allowed-tools,license,disable-model-invocation,user-invocable,argument-hint"
CURSOR_SKILL_FIELDS="name,description,license,compatibility,metadata,disable-model-invocation"
CLAUDE_SKILL_FIELDS="name,description,allowed-tools,disable-model-invocation,user-invocable,argument-hint,paths,context,agent,when_to_use,arguments,model,effort,hooks,shell"

COPILOT_PROMPT_FIELDS="name,description,agent"
CLAUDE_PROMPT_FIELDS="name,description"

COPILOT_AGENT_FIELDS="name,description,model,tools,disallowedTools,user-invocable,disable-model-invocation,target,argument-hint,agents,handoffs,mcp-servers,hooks,metadata"
CURSOR_AGENT_FIELDS="description,model,readonly,is_background"
CLAUDE_AGENT_FIELDS="name,description,model,tools,disallowedTools,mcp-servers,mcpServers,hooks,effort,maxTurns,permissionMode,memory,background,isolation,skills,color,initialPrompt"

# ═══════════════════════════════════════════
#  COPILOT
# ═══════════════════════════════════════════
sync_copilot() {
    local gh="$TARGET/.github"
    section "Copilot → $gh/"
    make_dir "$gh/instructions" "$gh/skills" "$gh/agents" "$gh/prompts"

    # instructions/*.instructions.md
    query_metadata "$SHARED_DIR/rules/metadata.yml" copilot "$COPILOT_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$gh/instructions/${name}.instructions.md" "$fm"
        info "instructions/${name}.instructions.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" copilot "$COPILOT_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        make_dir "$gh/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$gh/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    # plain-copy skills without metadata entry
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if ! already_synced ".github/skills/$s/SKILL.md"; then
            make_dir "$gh/skills/$s"; copy_file "$d/SKILL.md" "$gh/skills/$s/SKILL.md"
            info "skills/$s/ (plain)"
        fi
        copy_skill_subdirs "$d" "$gh/skills/$s"
    done

    # agents/*.agent.md
    query_metadata "$SHARED_DIR/agents/metadata.yml" copilot "$COPILOT_AGENT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/agents/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/agents/${name}.md" "$gh/agents/${name}.agent.md" "$fm"
        info "agents/${name}.agent.md"
    done
    # plain-copy agents without metadata entry
    for f in "$SHARED_DIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local a="$(basename "$f" .md)"
        [ "$a" = "metadata" ] && continue
        if ! already_synced ".github/agents/${a}.agent.md"; then
            copy_file "$f" "$gh/agents/${a}.agent.md"
            info "agents/${a}.agent.md (plain)"
        fi
    done

    # prompts/
    query_metadata "$SHARED_DIR/prompts/metadata.yml" copilot "$COPILOT_PROMPT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/prompts/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/prompts/${name}.md" "$gh/prompts/${name}.prompt.md" "$fm"
        info "prompts/${name}.prompt.md"
    done
}

# ═══════════════════════════════════════════
#  CURSOR
# ═══════════════════════════════════════════
sync_cursor() {
    local cur="$TARGET/.cursor"
    section "Cursor → $cur/"
    make_dir "$cur/rules" "$cur/skills" "$cur/agents"

    # rules/*.mdc (Cursor requires .mdc extension)
    query_metadata "$SHARED_DIR/rules/metadata.yml" cursor "$CURSOR_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cur/rules/${name}.mdc" "$fm"
        info "rules/${name}.mdc"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" cursor "$CURSOR_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        make_dir "$cur/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cur/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if ! already_synced ".cursor/skills/$s/SKILL.md"; then
            make_dir "$cur/skills/$s"; copy_file "$d/SKILL.md" "$cur/skills/$s/SKILL.md"
            info "skills/$s/ (plain)"
        fi
        copy_skill_subdirs "$d" "$cur/skills/$s"
    done

    # agents/*.md
    query_metadata "$SHARED_DIR/agents/metadata.yml" cursor "$CURSOR_AGENT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/agents/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/agents/${name}.md" "$cur/agents/${name}.md" "$fm"
        info "agents/${name}.md"
    done
    # plain-copy agents without metadata entry
    for f in "$SHARED_DIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local a="$(basename "$f" .md)"
        [ "$a" = "metadata" ] && continue
        if ! already_synced ".cursor/agents/${a}.md"; then
            copy_file "$f" "$cur/agents/${a}.md"
            info "agents/${a}.md (plain)"
        fi
    done
}

# ═══════════════════════════════════════════
#  CLAUDE
# ═══════════════════════════════════════════
sync_claude() {
    local cl="$TARGET/.claude"
    section "Claude → $cl/"
    make_dir "$cl/rules" "$cl/skills" "$cl/agents"

    # rules/*.md (with paths)
    query_metadata "$SHARED_DIR/rules/metadata.yml" claude "$CLAUDE_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cl/rules/${name}.md" "$fm"
        info "rules/${name}.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" claude "$CLAUDE_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        make_dir "$cl/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cl/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if ! already_synced ".claude/skills/$s/SKILL.md"; then
            make_dir "$cl/skills/$s"; copy_file "$d/SKILL.md" "$cl/skills/$s/SKILL.md"
            info "skills/$s/ (plain)"
        fi
        copy_skill_subdirs "$d" "$cl/skills/$s"
    done

    # agents/*.md — the ai-toolkit config stamps each agent's model/effort (#142)
    query_metadata "$SHARED_DIR/agents/metadata.yml" claude "$CLAUDE_AGENT_FIELDS" "$AI_TOOLKIT_CONFIG" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/agents/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/agents/${name}.md" "$cl/agents/${name}.md" "$fm"
        info "agents/${name}.md"
    done
    # plain-copy agents without metadata entry
    for f in "$SHARED_DIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local a="$(basename "$f" .md)"
        [ "$a" = "metadata" ] && continue
        if ! already_synced ".claude/agents/${a}.md"; then
            copy_file "$f" "$cl/agents/${a}.md"
            info "agents/${a}.md (plain)"
        fi
    done

    # prompts/
    query_metadata "$SHARED_DIR/prompts/metadata.yml" claude "$CLAUDE_PROMPT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/prompts/${name}.md" ] || continue
        make_dir "$cl/prompts"
        add_frontmatter "$SHARED_DIR/prompts/${name}.md" "$cl/prompts/${name}.md" "$fm"
        info "prompts/${name}.md"
    done
}

# ═══════════════════════════════════════════
#  HOOKS (shared across all platforms)
# ═══════════════════════════════════════════
sync_hooks() {
    local tool="$1"
    [ -f "$SHARED_DIR/hooks/metadata.yml" ] || return 0

    section "Hooks ($tool)"

    # Copy shared hook scripts to the platform-specific hooks/scripts/ dir
    local scripts_dst
    case "$tool" in
        copilot) scripts_dst="$TARGET/.github/hooks/scripts" ;;
        cursor)  scripts_dst="$TARGET/.cursor/hooks/scripts" ;;
        claude)  scripts_dst="$TARGET/.claude/hooks/scripts" ;;
    esac

    local scripts_rel="${scripts_dst#"$TARGET/"}"

    make_dir "$scripts_dst"
    for f in "$SHARED_DIR/hooks/"*.sh; do
        [ -f "$f" ] || continue
        copy_file "$f" "$scripts_dst/$(basename "$f")"
        if [ "$DRY_RUN" -eq 0 ]; then
            chmod +x "$scripts_dst/$(basename "$f")"
            info "hooks/scripts/$(basename "$f")"
        fi
    done

    # Copy shared lib/ utilities (inside scripts/ so $HOOK_DIR/lib/ resolves)
    if [ -d "$SHARED_DIR/hooks/lib" ]; then
        make_dir "$scripts_dst/lib"
        find "$SHARED_DIR/hooks/lib" -type f | while read -r f; do
            local rel="$scripts_rel/lib/${f#"$SHARED_DIR/hooks/lib/"}"
            record_file "$rel"
            if [ "$DRY_RUN" -eq 1 ]; then
                echo "[dry-run] would write $rel"
            fi
        done
        if [ "$DRY_RUN" -eq 0 ]; then
            cp -R "$SHARED_DIR/hooks/lib/"* "$scripts_dst/lib/" 2>/dev/null || true
            info "hooks/scripts/lib/"
        fi
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        # Generator/reconciler write .tmp/.bak files — skip entirely in dry-run.
        # Reconciler-owned configs (.cursor/hooks.json, .claude/settings.json)
        # are never recorded; only the copilot-owned ai-toolkit.json is.
        case "$tool" in
            copilot) record_file ".github/hooks/ai-toolkit.json"
                     echo "[dry-run] would write .github/hooks/ai-toolkit.json" ;;
            cursor)  echo "[dry-run] would reconcile .cursor/hooks.json" ;;
            claude)  echo "[dry-run] would reconcile .claude/settings.json" ;;
        esac
        return 0
    fi

    # Generate platform-specific hooks config JSON
    local json
    json=$(python3 "$SCRIPT_DIR/hooks_generator.py" "$SHARED_DIR/hooks" "$TARGET" "$tool")

    case "$tool" in
        copilot)
            local hooks_dir="$TARGET/.github/hooks"
            mkdir -p "$hooks_dir"
            echo "$json" > "$hooks_dir/ai-toolkit.json"
            record_file ".github/hooks/ai-toolkit.json"
            info "hooks/ai-toolkit.json"
            ;;
        cursor)
            local cursor_dir="$TARGET/.cursor"
            local cursor_file="$cursor_dir/hooks.json"
            mkdir -p "$cursor_dir"
            # Reconcile: ai-toolkit hooks are a managed/owned set. Existing owned
            # entries are removed and replaced with the fresh set exactly once;
            # user-authored hooks are preserved. Converges to a fixed point.
            # NOT recorded in the manifest: reconciler-owned (and GC-protected).
            [ -f "$cursor_file" ] && cp "$cursor_file" "$cursor_file.bak"
            python3 "$SCRIPT_DIR/hooks_reconciler.py" cursor \
                "${cursor_file}" <<< "$json" > "$cursor_file.tmp"
            mv "$cursor_file.tmp" "$cursor_file"
            info "hooks.json"
            ;;
        claude)
            local claude_dir="$TARGET/.claude"
            local claude_file="$claude_dir/settings.json"
            mkdir -p "$claude_dir"
            # Reconcile ai-toolkit hooks into settings.json (ownership-aware,
            # idempotent) — replaces the old pure-append .extend() that grew the
            # file without bound on every sync.
            # NOT recorded in the manifest: reconciler-owned (and GC-protected).
            [ -f "$claude_file" ] && cp "$claude_file" "$claude_file.bak"
            python3 "$SCRIPT_DIR/hooks_reconciler.py" claude \
                "${claude_file}" <<< "$json" > "$claude_file.tmp"
            mv "$claude_file.tmp" "$claude_file"
            info "settings.json (hooks)"
            ;;
    esac
}

# ═══════════════════════════════════════════
#  MCP SERVERS (shared across all platforms)
# ═══════════════════════════════════════════
# The code-review agent frontmatter references the review-stamp server at
# ./.ai-toolkit/mcp/review-stamp/run.sh — a repo-relative path that must
# resolve in the TARGET repo, not just the toolkit. Install the server there.
# Source lives at the toolkit repo root (mcp/), not under shared/.
sync_mcp_servers() {
    local src_dir="$REPO_DIR/mcp/review-stamp"
    [ -d "$src_dir" ] || return 0

    section "MCP servers"
    local dst_dir="$TARGET/.ai-toolkit/mcp/review-stamp"
    make_dir "$dst_dir"
    local f
    for f in run.sh server.py; do
        [ -f "$src_dir/$f" ] || continue
        copy_file "$src_dir/$f" "$dst_dir/$f"
        if [ "$DRY_RUN" -eq 0 ]; then
            if [ "$f" = "run.sh" ]; then
                chmod +x "$dst_dir/$f"
            fi
            info "mcp/review-stamp/$f"
        fi
    done
}

# ═══════════════════════════════════════════
#  WORKFLOW SCRIPTS (hub / spoke / land)
# ═══════════════════════════════════════════
# The hub/start-task/land skills invoke the parallel-worktrees scripts at
# .ai-toolkit/scripts/<name> — a canonical location that resolves in both the
# ai-toolkit checkout and a synced target (same convention as .ai-toolkit/mcp/).
# Install them there so the whole flow works after a plain sync, with no manual
# steps. The worktree scripts source each other via $SCRIPT_DIR (BASH_SOURCE),
# so co-locating all four keeps the cross-references intact; they locate the
# main checkout by git introspection, so they run unmodified from here.
# spoke-push.sh ships here too so the spoke's PUSH step runs as one
# allowlistable process (issue #37); spoke-ready.sh likewise, so marker emission
# (ready/N, gate/N) is one allowlistable command (issue #45).
# telemetry-ingest-spoke.sh ships so the synced land script can call it at
# teardown for the #87/#92 post-run Langfuse ingestion; worktree-quick.sh ships
# so the /quick express lane resolves in a synced target.
# hub-afk.sh and batch-plan.sh ship so /afk (issue #71) and /next-batch
# (issue #70) resolve in a synced target like their hub-status.sh siblings.
# gate-sweep.sh ships so the land tail's conditional post-land sweep
# (issue #124) can launch from the runtime dir.
# travel-local.sh ships so `travel-local on|off|status` (issue #248) resolves in
# a synced target; it lives at the toolkit-root scripts/, so it takes the loop's
# default source case (src="$SCRIPT_DIR/$name") like the worktree scripts.
# Sources: worktree-*.sh, spoke-push.sh, spoke-ready.sh and
# telemetry-ingest-spoke.sh at the toolkit root scripts/, hub-status.sh,
# hub-ready-watch.sh, hub-otel-watch.sh, hub-afk.sh and batch-plan.sh under
# the hub skill (shared/skills/hub/scripts/).
sync_workflow_scripts() {
    section "Workflow scripts (hub/spoke/land)"
    local dst_dir="$TARGET/.ai-toolkit/scripts"
    make_dir "$dst_dir"

    # telemetry.sh and base-branch.sh are co-located here (not only under
    # hooks/lib/) so the worktree scripts can source them as siblings — see
    # worktree-lib.sh's telemetry and base-branch blocks.
    local name src
    for name in worktree-new.sh worktree-land.sh worktree-done.sh worktree-lib.sh worktree-quick.sh spoke-push.sh spoke-ready.sh spoke-relaunch.sh gate-sweep.sh travel-local.sh telemetry-ingest-spoke.sh hub-status.sh hub-ready-watch.sh hub-notify.sh hub-otel-watch.sh hub-afk.sh hub-afk-land.sh hub-afk-dispatch.sh hub-afk-arm.sh hub-afk-supervise.sh hub-agent.sh gate-broker.sh gate-broker-markers.sh gate-broker-detect.sh gate-broker-classify.sh gate-broker-danger.sh gate-broker-answerer.sh gate-broker-permission.sh transition-log.sh hub-inject.sh hub-watchdog.sh batch-plan.sh telemetry.sh base-branch.sh enabled.sh; do
        case "$name" in
            hub-status.sh|hub-ready-watch.sh|hub-notify.sh|hub-otel-watch.sh|hub-afk.sh|hub-afk-land.sh|hub-afk-dispatch.sh|hub-afk-arm.sh|hub-afk-supervise.sh|hub-agent.sh|gate-broker.sh|gate-broker-markers.sh|gate-broker-detect.sh|gate-broker-classify.sh|gate-broker-danger.sh|gate-broker-answerer.sh|gate-broker-permission.sh|transition-log.sh|hub-inject.sh|hub-watchdog.sh|batch-plan.sh) src="$SHARED_DIR/skills/hub/scripts/$name" ;;
            telemetry.sh|base-branch.sh|enabled.sh)      src="$SHARED_DIR/hooks/lib/$name" ;;
            *)                                src="$SCRIPT_DIR/$name" ;;
        esac
        [ -f "$src" ] || continue
        copy_file "$src" "$dst_dir/$name"
        if [ "$DRY_RUN" -eq 0 ]; then
            chmod +x "$dst_dir/$name"
            info "scripts/$name"
        fi
    done

    # Spoke-default model env (issue #142), derived from the config's
    # `model.spoke`: emits WT_AGENT_MODEL_DEFAULT / WT_AGENT_EFFORT_DEFAULT. The
    # consumer — worktree-new.sh sourcing this to pin the spoke driver's model
    # off the config instead of a hardcoded literal — is wired in the same
    # issue's worktree-new change; this is the producer half.
    record_file ".ai-toolkit/scripts/spoke-model.env"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would write .ai-toolkit/scripts/spoke-model.env"
    else
        # Render into a var first so a config-read failure surfaces without
        # truncating the target file to 0 bytes mid-sync (matches the
        # graceful degradation of apply_base_branch_config below).
        local spoke_env
        if spoke_env="$(python3 "$SCRIPT_DIR/ai_toolkit_config.py" spoke-env "$AI_TOOLKIT_CONFIG")"; then
            printf '%s\n' "$spoke_env" > "$dst_dir/spoke-model.env"
            info "scripts/spoke-model.env"
        else
            warn "could not render spoke-model.env from $AI_TOOLKIT_CONFIG — skipped"
        fi
    fi
}

# ═══════════════════════════════════════════
#  SHARED CONFIG FILES (pyproject.toml, etc.)
# ═══════════════════════════════════════════
sync_config_files() {
    section "Config files"

    # Generic helper: copy a shared config file if target doesn't have one.
    # NOT recorded in the manifest: copy-if-absent semantics mean the user
    # owns the file after creation, so GC must never reclaim it.
    _sync_config() {
        local filename="$1"
        [ -f "$SHARED_DIR/$filename" ] || return 0
        if [ -f "$TARGET/$filename" ]; then
            warn "$filename already exists in target — skipped (merge manually if needed)"
        elif [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would write $filename"
        else
            cp "$SHARED_DIR/$filename" "$TARGET/$filename"
            info "$filename"
        fi
    }

    _sync_config "pyproject.toml"
    _sync_config "ruff.toml"
    _sync_config ".gitignore"
    _sync_config ".editorconfig"
    _sync_config ".python-version"
}

# ═══════════════════════════════════════════
#  BASE BRANCH (issue #142)
# ═══════════════════════════════════════════
# Apply the config's `base_branch` to `git config ai-toolkit.base-branch` on the
# target, so the canonical wt_base_branch resolver (issue #117) — consumed by
# worktree-new/land/done/quick, the hub scripts and every guard — picks it up
# unchanged. A NON-EMPTY base_branch is authoritative and is written through.
#
# An EMPTY/absent base_branch does NOT clobber the target (issue #309): ai-toolkit
# ships an empty base_branch, so unsetting on every re-sync would wipe a
# downstream's own per-project ai-toolkit.base-branch — the resolver's tier-1,
# re-sync-proof seat. So empty leaves the target's key untouched; a downstream/
# operator that wants auto-detection back unsets ai-toolkit.base-branch itself.
#
# Also warns (issue #309) when the target carries a mis-cased ai-toolkit.baseBranch
# but not the hyphenated key the resolver reads — a silently-ignored footgun.
apply_base_branch_config() {
    [ -e "$TARGET/.git" ] || return 0   # only a git repo carries config
    local bb
    bb="$(python3 "$SCRIPT_DIR/ai_toolkit_config.py" base-branch "$AI_TOOLKIT_CONFIG" 2>/dev/null || true)"
    section "Base branch (ai-toolkit.base-branch)"
    if [ -n "$bb" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would set git config ai-toolkit.base-branch=$bb"
        else
            git -C "$TARGET" config ai-toolkit.base-branch "$bb"
            info "base-branch → $bb"
        fi
    elif [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] base_branch empty in config — leaving target's ai-toolkit.base-branch untouched"
    else
        local own
        own="$(git -C "$TARGET" config --get ai-toolkit.base-branch 2>/dev/null || true)"
        if [ -n "$own" ]; then
            info "base-branch → $own (preserved; empty in config, downstream's own value kept)"
        else
            info "base-branch → auto-detect (empty in config; nothing to preserve)"
        fi
    fi
    warn_base_branch_camelcase
}

# Warn (issue #309) when the target carries a camelCase ai-toolkit.baseBranch (git
# flattens it to the distinct key `basebranch`) but NOT the hyphenated
# ai-toolkit.base-branch the wt_base_branch resolver actually reads — a silently
# ignored footgun. Loud here (fail-loud, AFK principle #2) so the mis-cased set is
# caught at sync time rather than surfacing as a wrong resolved branch later.
warn_base_branch_camelcase() {
    [ -e "$TARGET/.git" ] || return 0
    if git -C "$TARGET" config --get ai-toolkit.basebranch >/dev/null 2>&1 \
        && ! git -C "$TARGET" config --get ai-toolkit.base-branch >/dev/null 2>&1; then
        warn "git config ai-toolkit.baseBranch is set but the resolver reads ai-toolkit.base-branch (hyphenated); the camelCase key is IGNORED. Run: git config ai-toolkit.base-branch \"<branch>\""
    fi
}

# Apply the config's `enabled` to `git config ai-toolkit.enabled` on the target —
# the DURABLE default tier the ai_toolkit_enabled resolver reads (issue #154). The
# config OWNS this value: `false` sets the disabled default, `true`/absent clears
# the key so the resolver defaults to ENABLED. It deliberately does NOT touch the
# <git-common-dir>/ai-toolkit-off marker (the sync-safe quick flip that WINS over
# this default) so a manual `ai-toolkit off` survives a re-sync.
apply_enabled_config() {
    [ -e "$TARGET/.git" ] || return 0   # only a git repo carries config
    local en
    en="$(python3 "$SCRIPT_DIR/ai_toolkit_config.py" enabled "$AI_TOOLKIT_CONFIG" 2>/dev/null || true)"
    section "Global on/off switch (ai-toolkit.enabled)"
    if [ "$en" = "false" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would set git config ai-toolkit.enabled=false (toolkit OFF by default)"
        else
            git -C "$TARGET" config ai-toolkit.enabled false
            warn "enabled → false — toolkit OFF by default (gates/guards/telemetry bypassed)"
        fi
    elif [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would clear git config ai-toolkit.enabled (default ON)"
    else
        # true / absent / unreadable ⇒ clear, so the resolver defaults to ENABLED
        # (the safe direction — a broken config never silently disables the cage).
        git -C "$TARGET" config --unset ai-toolkit.enabled 2>/dev/null || true
        info "enabled → on (unset; full enforcement)"
    fi
}

# ═══════════════════════════════════════════
#  SYNC MANIFEST + STALE-FILE GC
# ═══════════════════════════════════════════
TOOLKIT_REV="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Pipe the recorded file list to sync_manifest.py: updates the per-tool list
# in .ai-toolkit-manifest.json and deletes files a previous sync wrote but
# this one didn't (printed one per line on stdout).
finalize_manifest() {
    local tool="$1"
    local args=(finalize "$TARGET" "$tool" "$TOOLKIT_REV")
    [ "$DRY_RUN" -eq 1 ] && args+=(--dry-run)
    local deleted p
    deleted="$(sort -u "$RECORD_FILE" | python3 "$SCRIPT_DIR/sync_manifest.py" "${args[@]}")"
    [ -n "$deleted" ] || return 0
    while IFS= read -r p; do
        if [ "$DRY_RUN" -eq 1 ]; then
            info "GC [dry-run] would delete stale $p"
        else
            info "GC deleted stale $p"
        fi
    done <<< "$deleted"
}

# Run one tool's sync + hooks with a fresh record file, then finalize.
# The record file is a temp FILE (not a bash array) because the sync loops
# run in `| while read` pipeline subshells — file appends survive, array
# appends don't.
sync_tool() {
    local tool="$1"
    RECORD_FILE="$(mktemp)"
    case "$tool" in
        copilot) sync_copilot ;;
        cursor)  sync_cursor ;;
        claude)  sync_claude ;;
    esac
    sync_hooks "$tool"
    sync_mcp_servers
    sync_workflow_scripts
    finalize_manifest "$tool"
    rm -f "$RECORD_FILE"
    RECORD_FILE=""
}

# ═══════════════════════════════════════════
#  NATIVE GIT HOOKS (fallback enforcement layer)
# ═══════════════════════════════════════════
# The cage scripts normally run as agent preToolUse hooks, which depends on the
# agent runtime invoking them. Installing them as NATIVE git hooks makes the
# blocking gates fire on real `git commit` / `git push` regardless of who drives
# git (agent, human, CI). This is an opt-in fallback layer (--with-git-hooks).
sync_git_hooks() {
    section "Native git hooks (fallback enforcement)"
    if [ ! -d "$TARGET/.git" ]; then
        warn "Target is not a git repository — skipping native git hooks"
        return 0
    fi
    if [ ! -x "$SCRIPT_DIR/install-git-hooks.sh" ]; then
        warn "install-git-hooks.sh not found or not executable — skipping"
        return 0
    fi
    bash "$SCRIPT_DIR/install-git-hooks.sh" "$TARGET"
}

# --local-only: exclude the ai-toolkit-managed paths from the target repo LOCALLY, so a
# personal deployment never gets committed or propagated to teammates. Uses the target's
# .git/info/exclude (per-clone, NOT tracked) rather than .gitignore (which would itself be a
# committed change). Idempotent: a marked block is replaced, never duplicated. Surgical about
# .github — only the ai-toolkit SUBDIRS are excluded, never .github/ or .github/workflows/
# (the project's real CI). Resolves the git common dir so it also works from inside a worktree.
sync_local_exclude() {
    section "Local git exclude (personal, not committed)"
    local common exclude
    common="$(git -C "$TARGET" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
        warn "Target is not a git repository — skipping --local-only exclude"
        return 0
    }
    exclude="$common/info/exclude"
    local begin="# >>> ai-toolkit (local, personal — do not commit) >>>"
    local end="# <<< ai-toolkit <<<"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would write the ai-toolkit block to $exclude"
        return 0
    fi
    make_dir "$common/info"
    # Strip any prior block (idempotent), then append a fresh one.
    if [ -f "$exclude" ]; then
        awk -v b="$begin" -v e="$end" '
            $0==b {skip=1} skip && $0==e {skip=0; next} !skip {print}
        ' "$exclude" > "$exclude.tmp" && mv "$exclude.tmp" "$exclude"
    fi
    {
        printf '%s\n' "$begin"
        printf '/.claude/\n/.cursor/\n/.ai-toolkit/\n'
        printf '/.github/instructions/\n/.github/skills/\n/.github/agents/\n/.github/prompts/\n/.github/hooks/\n'
        printf '.testmondata\n.testmondata-shm\n.testmondata-wal\n'
        printf '%s\n' "$end"
    } >> "$exclude"
    info "wrote local exclude block to .git/info/exclude (ai-toolkit paths ignored on this clone only)"
}

# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Sync to Repo              ║"
echo "╚══════════════════════════════════════════╝"

case "$TOOL" in
    copilot|cursor|claude) sync_tool "$TOOL"; sync_config_files ;;
    all)     sync_tool copilot; sync_tool cursor; sync_tool claude; sync_config_files ;;
    *)       error "Unknown tool: $TOOL"; usage ;;
esac

apply_base_branch_config
apply_enabled_config

if [ "$WITH_GIT_HOOKS" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    sync_git_hooks
fi

if [ "$LOCAL_ONLY" -eq 1 ]; then
    sync_local_exclude
fi

echo ""
if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] No changes were made to $TARGET"
elif [ "$WITH_GIT_HOOKS" -eq 1 ]; then
    info "Sync complete! Review changes with: cd $TARGET && git diff"
    info "Native git hooks installed as a fallback enforcement layer."
    echo "  Uninstall with: $SCRIPT_DIR/install-git-hooks.sh --uninstall $TARGET"
else
    info "Sync complete! Review changes with: cd $TARGET && git diff"
    warn "Agent hooks only. For enforcement independent of the agent runtime, re-run with --with-git-hooks"
    echo "  (installs commit-quality + commit-gauntlet as native git hooks)"
fi

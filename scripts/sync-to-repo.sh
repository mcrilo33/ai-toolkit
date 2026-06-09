#!/usr/bin/env bash
# sync-to-repo.sh — Generate tool-specific configs from shared/ into a target repo.
# Reads per-directory metadata.yml for frontmatter. Python 3 stdlib only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SHARED_DIR="$REPO_DIR/shared"

GREEN='\033[0;32m'  YELLOW='\033[1;33m'  RED='\033[0;31m'  BLUE='\033[0;34m'  NC='\033[0m'
info()    { echo -e "${GREEN}✓${NC} $1"; }
warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
error()   { echo -e "${RED}✗${NC} $1" >&2; }
section() { echo -e "\n${BLUE}── $1 ──${NC}"; }

usage() {
    echo "Usage: $0 <target-repo-path> [tool] [--with-git-hooks]"
    echo ""
    echo "Tools: copilot, cursor, claude, all (default: all)"
    echo ""
    echo "Flags:"
    echo "  --with-git-hooks   Also install the cage scripts as NATIVE git hooks"
    echo "                     (fallback enforcement, independent of the agent runtime)"
    echo ""
    echo "Examples:"
    echo "  $0 ~/Repos/my-project                       # Sync all tools"
    echo "  $0 ~/Repos/my-project copilot                # Copilot only"
    echo "  $0 ~/Repos/my-project cursor                 # Cursor only"
    echo "  $0 ~/Repos/my-project claude                 # Claude only"
    echo "  $0 ~/Repos/my-project all --with-git-hooks   # Sync + native git hooks"
    exit 1
}

[ $# -lt 1 ] && usage
TARGET=""
TOOL="all"
WITH_GIT_HOOKS=0
for arg in "$@"; do
    case "$arg" in
        --with-git-hooks) WITH_GIT_HOOKS=1 ;;
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

if [ ! -d "$TARGET/.git" ]; then
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
    local meta_file="$1" tool="$2" fields="$3"
    [ -f "$meta_file" ] || return 0
    python3 "$SCRIPT_DIR/metadata_parser.py" "$meta_file" "$tool" "$fields"
}

# ─── Helper: write file with YAML frontmatter ───
add_frontmatter() {
    local src="$1" dst="$2" meta="$3"
    { echo "---"; echo -e "$meta"; echo "---"; echo ""; cat "$src"; } > "$dst"
}

# ─── Helper: copy skill subdirectories (references, scripts, templates, assets) ───
copy_skill_subdirs() {
    local src_dir="$1" dst_dir="$2"
    for subdir in references scripts templates assets; do
        [ -d "$src_dir/$subdir" ] || continue
        cp -R "$src_dir/$subdir" "$dst_dir/"
        info "  └── $subdir/"
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
CLAUDE_AGENT_FIELDS="name,description,model,tools,disallowedTools,mcp-servers,hooks,effort,maxTurns,permissionMode,memory,background,isolation,skills,color,initialPrompt"

# ═══════════════════════════════════════════
#  COPILOT
# ═══════════════════════════════════════════
sync_copilot() {
    local gh="$TARGET/.github"
    section "Copilot → $gh/"
    mkdir -p "$gh/instructions" "$gh/skills" "$gh/agents" "$gh/prompts"

    # instructions/*.instructions.md
    query_metadata "$SHARED_DIR/rules/metadata.yml" copilot "$COPILOT_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$gh/instructions/${name}.instructions.md" "$fm"
        info "instructions/${name}.instructions.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" copilot "$COPILOT_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$gh/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$gh/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    # plain-copy skills without metadata entry
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if [ ! -f "$gh/skills/$s/SKILL.md" ]; then
            mkdir -p "$gh/skills/$s"; cp "$d/SKILL.md" "$gh/skills/$s/SKILL.md"
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
        if [ ! -f "$gh/agents/${a}.agent.md" ]; then
            cp "$f" "$gh/agents/${a}.agent.md"
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
    mkdir -p "$cur/rules" "$cur/skills" "$cur/agents"

    # rules/*.mdc (Cursor requires .mdc extension)
    query_metadata "$SHARED_DIR/rules/metadata.yml" cursor "$CURSOR_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cur/rules/${name}.mdc" "$fm"
        info "rules/${name}.mdc"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" cursor "$CURSOR_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$cur/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cur/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if [ ! -f "$cur/skills/$s/SKILL.md" ]; then
            mkdir -p "$cur/skills/$s"; cp "$d/SKILL.md" "$cur/skills/$s/SKILL.md"
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
        if [ ! -f "$cur/agents/${a}.md" ]; then
            cp "$f" "$cur/agents/${a}.md"
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
    mkdir -p "$cl/rules" "$cl/skills" "$cl/agents"

    # rules/*.md (with paths)
    query_metadata "$SHARED_DIR/rules/metadata.yml" claude "$CLAUDE_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cl/rules/${name}.md" "$fm"
        info "rules/${name}.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" claude "$CLAUDE_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$cl/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cl/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if [ ! -f "$cl/skills/$s/SKILL.md" ]; then
            mkdir -p "$cl/skills/$s"; cp "$d/SKILL.md" "$cl/skills/$s/SKILL.md"
            info "skills/$s/ (plain)"
        fi
        copy_skill_subdirs "$d" "$cl/skills/$s"
    done

    # agents/*.md
    query_metadata "$SHARED_DIR/agents/metadata.yml" claude "$CLAUDE_AGENT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/agents/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/agents/${name}.md" "$cl/agents/${name}.md" "$fm"
        info "agents/${name}.md"
    done
    # plain-copy agents without metadata entry
    for f in "$SHARED_DIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local a="$(basename "$f" .md)"
        [ "$a" = "metadata" ] && continue
        if [ ! -f "$cl/agents/${a}.md" ]; then
            cp "$f" "$cl/agents/${a}.md"
            info "agents/${a}.md (plain)"
        fi
    done

    # prompts/
    query_metadata "$SHARED_DIR/prompts/metadata.yml" claude "$CLAUDE_PROMPT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/prompts/${name}.md" ] || continue
        mkdir -p "$cl/prompts"
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

    mkdir -p "$scripts_dst"
    for f in "$SHARED_DIR/hooks/"*.sh; do
        [ -f "$f" ] || continue
        cp "$f" "$scripts_dst/"
        chmod +x "$scripts_dst/$(basename "$f")"
        info "hooks/scripts/$(basename "$f")"
    done

    # Copy shared lib/ utilities (inside scripts/ so $HOOK_DIR/lib/ resolves)
    if [ -d "$SHARED_DIR/hooks/lib" ]; then
        mkdir -p "$scripts_dst/lib"
        cp -R "$SHARED_DIR/hooks/lib/"* "$scripts_dst/lib/" 2>/dev/null || true
        info "hooks/scripts/lib/"
    fi

    # Generate platform-specific hooks config JSON
    local json
    json=$(python3 "$SCRIPT_DIR/hooks_generator.py" "$SHARED_DIR/hooks" "$TARGET" "$tool")

    case "$tool" in
        copilot)
            local hooks_dir="$TARGET/.github/hooks"
            mkdir -p "$hooks_dir"
            echo "$json" > "$hooks_dir/ai-toolkit.json"
            info "hooks/ai-toolkit.json"
            ;;
        cursor)
            local cursor_dir="$TARGET/.cursor"
            local cursor_file="$cursor_dir/hooks.json"
            mkdir -p "$cursor_dir"
            # Reconcile: ai-toolkit hooks are a managed/owned set. Existing owned
            # entries are removed and replaced with the fresh set exactly once;
            # user-authored hooks are preserved. Converges to a fixed point.
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
            [ -f "$claude_file" ] && cp "$claude_file" "$claude_file.bak"
            python3 "$SCRIPT_DIR/hooks_reconciler.py" claude \
                "${claude_file}" <<< "$json" > "$claude_file.tmp"
            mv "$claude_file.tmp" "$claude_file"
            info "settings.json (hooks)"
            ;;
    esac
}

# ═══════════════════════════════════════════
#  SHARED CONFIG FILES (pyproject.toml, etc.)
# ═══════════════════════════════════════════
sync_config_files() {
    section "Config files"

    # Generic helper: copy a shared config file if target doesn't have one
    _sync_config() {
        local filename="$1"
        if [ -f "$SHARED_DIR/$filename" ]; then
            if [ -f "$TARGET/$filename" ]; then
                warn "$filename already exists in target — skipped (merge manually if needed)"
            else
                cp "$SHARED_DIR/$filename" "$TARGET/$filename"
                info "$filename"
            fi
        fi
    }

    _sync_config "pyproject.toml"
    _sync_config "ruff.toml"
    _sync_config ".gitignore"
    _sync_config ".editorconfig"
    _sync_config ".python-version"
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

# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Sync to Repo              ║"
echo "╚══════════════════════════════════════════╝"

case "$TOOL" in
    copilot) sync_copilot; sync_hooks copilot; sync_config_files ;;
    cursor)  sync_cursor;  sync_hooks cursor;  sync_config_files ;;
    claude)  sync_claude;  sync_hooks claude;  sync_config_files ;;
    all)     sync_copilot; sync_hooks copilot; sync_cursor; sync_hooks cursor; sync_claude; sync_hooks claude; sync_config_files ;;
    *)       error "Unknown tool: $TOOL"; usage ;;
esac

if [ "$WITH_GIT_HOOKS" -eq 1 ]; then
    sync_git_hooks
fi

echo ""
info "Sync complete! Review changes with: cd $TARGET && git diff"
if [ "$WITH_GIT_HOOKS" -eq 1 ]; then
    info "Native git hooks installed as a fallback enforcement layer."
    echo "  Uninstall with: $SCRIPT_DIR/install-git-hooks.sh --uninstall $TARGET"
else
    warn "Agent hooks only. For enforcement independent of the agent runtime, re-run with --with-git-hooks"
    echo "  (installs commit-quality + commit-gauntlet as native git hooks)"
fi

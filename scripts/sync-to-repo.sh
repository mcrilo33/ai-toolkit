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
    echo "Usage: $0 <target-repo-path> [tool]"
    echo ""
    echo "Tools: copilot, cursor, claude, all (default: all)"
    echo ""
    echo "Examples:"
    echo "  $0 ~/Repos/my-project              # Sync all tools"
    echo "  $0 ~/Repos/my-project copilot       # Copilot only"
    echo "  $0 ~/Repos/my-project cursor        # Cursor only"
    echo "  $0 ~/Repos/my-project claude        # Claude only"
    exit 1
}

[ $# -lt 1 ] && usage
TARGET="$1"
TOOL="${2:-all}"
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

    # Copy shared lib/ utilities
    if [ -d "$SHARED_DIR/hooks/lib" ]; then
        mkdir -p "$scripts_dst/../lib"
        cp -R "$SHARED_DIR/hooks/lib/"* "$scripts_dst/../lib/" 2>/dev/null || true
        info "hooks/lib/"
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
            mkdir -p "$cursor_dir"
            # Merge into existing hooks.json or create new
            if [ -f "$cursor_dir/hooks.json" ]; then
                # Preserve existing hooks, merge ai-toolkit hooks
                python3 -c "
import json, sys
with open('$cursor_dir/hooks.json') as f:
    existing = json.load(f)
new = json.loads(sys.stdin.read())
for event, hooks in new.get('hooks', {}).items():
    existing.setdefault('hooks', {}).setdefault(event, []).extend(hooks)
existing['version'] = 1
print(json.dumps(existing, indent=2))
" <<< "$json" > "$cursor_dir/hooks.json.tmp"
                mv "$cursor_dir/hooks.json.tmp" "$cursor_dir/hooks.json"
            else
                echo "$json" > "$cursor_dir/hooks.json"
            fi
            info "hooks.json"
            ;;
        claude)
            local claude_dir="$TARGET/.claude"
            mkdir -p "$claude_dir"
            # Merge hooks into .claude/settings.json
            if [ -f "$claude_dir/settings.json" ]; then
                python3 -c "
import json, sys
with open('$claude_dir/settings.json') as f:
    settings = json.load(f)
new_hooks = json.loads(sys.stdin.read())
settings.setdefault('hooks', {})
for event, groups in new_hooks.items():
    settings['hooks'].setdefault(event, []).extend(groups)
print(json.dumps(settings, indent=2))
" <<< "$json" > "$claude_dir/settings.json.tmp"
                mv "$claude_dir/settings.json.tmp" "$claude_dir/settings.json"
            else
                echo "{\"hooks\": $json}" | python3 -c "
import json, sys
print(json.dumps(json.load(sys.stdin), indent=2))
" > "$claude_dir/settings.json"
            fi
            info "settings.json (hooks)"
            ;;
    esac
}

# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Sync to Repo              ║"
echo "╚══════════════════════════════════════════╝"

case "$TOOL" in
    copilot) sync_copilot; sync_hooks copilot ;;
    cursor)  sync_cursor;  sync_hooks cursor ;;
    claude)  sync_claude;  sync_hooks claude ;;
    all)     sync_copilot; sync_hooks copilot; sync_cursor; sync_hooks cursor; sync_claude; sync_hooks claude ;;
    *)       error "Unknown tool: $TOOL"; usage ;;
esac

echo ""
info "Sync complete! Review changes with: cd $TARGET && git diff"

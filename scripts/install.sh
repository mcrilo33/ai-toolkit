#!/usr/bin/env bash
# install.sh — Symlink ai-toolkit configs to their expected locations
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }

link_file() {
    local src="$1"
    local dst="$2"

    if [ -L "$dst" ]; then
        local current
        current="$(readlink "$dst")"
        if [ "$current" = "$src" ]; then
            info "Already linked: $dst"
            return
        fi
        warn "Replacing symlink: $dst (was → $current)"
        rm "$dst"
    elif [ -e "$dst" ]; then
        warn "Backing up existing: $dst → ${dst}.bak"
        mv "$dst" "${dst}.bak"
    fi

    mkdir -p "$(dirname "$dst")"
    ln -s "$src" "$dst"
    info "Linked: $dst → $src"
}

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Install Symlinks          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# --- Cursor Rules ---
echo "── Cursor Rules ──"
mkdir -p "$HOME/.cursor/rules"
for f in "$REPO_DIR/cursor/rules/"*.mdc; do
    [ -f "$f" ] || continue
    link_file "$f" "$HOME/.cursor/rules/$(basename "$f")"
done

# --- Cursor Skills ---
echo ""
echo "── Cursor Skills ──"
mkdir -p "$HOME/.cursor/skills"
for d in "$REPO_DIR/cursor/skills/"*/; do
    [ -d "$d" ] || continue
    skill="$(basename "$d")"
    link_file "$d" "$HOME/.cursor/skills/$skill"
done

# --- Cursor MCP ---
echo ""
echo "── Cursor MCP ──"
link_file "$REPO_DIR/settings/cursor/mcp.json" "$HOME/.cursor/mcp.json"

# --- Claude Settings ---
echo ""
echo "── Claude Code ──"
mkdir -p "$HOME/.claude"
link_file "$REPO_DIR/claude/settings.json" "$HOME/.claude/settings.json"

# --- Claude Skills ---
mkdir -p "$HOME/.claude/skills"
for d in "$REPO_DIR/claude/skills/"*/; do
    [ -d "$d" ] || continue
    skill="$(basename "$d")"
    link_file "$d" "$HOME/.claude/skills/$skill"
done

# --- VS Code (instructions only, can't symlink partial settings.json) ---
echo ""
echo "── VS Code / Copilot ──"
warn "VS Code settings cannot be symlinked (partial settings.json)."
echo "  Copy relevant keys from:"
echo "    $REPO_DIR/settings/vscode/copilot-settings.jsonc"
echo "  MCP config:"
echo "    $REPO_DIR/settings/vscode/mcp.json"
echo "  Custom language models:"
echo "    $REPO_DIR/settings/vscode/chat-language-models.json"
echo ""
echo "  For per-repo Copilot instructions, use:"
echo "    ./scripts/sync-to-repo.sh <repo-path>"

echo ""
info "Installation complete!"

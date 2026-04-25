#!/usr/bin/env bash
# install.sh — Install ai-toolkit settings and symlinks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

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
echo "║   ai-toolkit — Install Settings           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# --- Cursor MCP ---
echo "── Cursor MCP ──"
if [ -f "$REPO_DIR/settings/cursor/mcp.json" ]; then
    link_file "$REPO_DIR/settings/cursor/mcp.json" "$HOME/.cursor/mcp.json"
fi

# --- Claude Settings ---
echo ""
echo "── Claude Code ──"
if [ -f "$REPO_DIR/claude/settings.json" ]; then
    mkdir -p "$HOME/.claude"
    link_file "$REPO_DIR/claude/settings.json" "$HOME/.claude/settings.json"
fi

# --- VS Code ---
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
echo "── Per-Repo Tool Configs ──"
echo "  To sync Copilot, Cursor, and Claude configs into a project:"
echo ""
echo "    ./scripts/sync-to-repo.sh <repo-path>          # All tools"
echo "    ./scripts/sync-to-repo.sh <repo-path> copilot   # Copilot only"
echo "    ./scripts/sync-to-repo.sh <repo-path> cursor    # Cursor only"
echo "    ./scripts/sync-to-repo.sh <repo-path> claude    # Claude only"

echo ""
info "Installation complete!"

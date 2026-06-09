#!/usr/bin/env bash
# config-protection — block agent edits to linter/formatter/CI/lockfile config.
# Prevents agents from "fixing" project configuration without asking.
#
# Two entry points, one script (shared across platforms):
#
#   • Cursor (beforeShellExecution): on `git add` / `git commit`, inspect the
#     STAGED file list (git diff --cached --name-only) and DENY if any staged
#     path is a protected config file. This replaces the old Cursor Write-time
#     block, which fired on an internal scratch payload and never saw the real
#     file.
#
#   • Claude/Copilot (preToolUse / Write|Edit): inspect the file being written
#     (tool_input.file_path) and DENY before the write lands.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

# ── Protected config files ──────────────────────────────────────────
PROTECTED_FILES=(
  # Linters
  ".eslintrc" ".eslintrc.json" ".eslintrc.js" ".eslintrc.yml"
  "eslint.config.js" "eslint.config.mjs" "eslint.config.ts"
  ".flake8" ".pylintrc"
  # Formatters
  ".prettierrc" ".prettierrc.json" ".prettierrc.yml" ".prettierrc.js"
  "prettier.config.js" "prettier.config.mjs"
  "biome.json" "biome.jsonc"
  ".editorconfig" ".clang-format"
  # TypeScript / Build
  "tsconfig.json" "tsconfig.build.json"
  # CI/CD
  ".github/workflows" ".gitlab-ci.yml"
  # Package managers (lockfiles)
  "package-lock.json" "yarn.lock" "pnpm-lock.yaml" "bun.lockb"
  "Pipfile.lock" "poetry.lock" "uv.lock"
  "Cargo.lock" "go.sum" "Gemfile.lock"
  # Python config
  "pyproject.toml" "setup.cfg" "setup.py"
)

# matched_protected <path> → prints the matching protected name (and returns 0)
# or returns 1 if the path is not protected. Matches by basename equality or
# substring (covers ".github/workflows" directory paths).
matched_protected() {
  local path="$1" base protected
  base=$(basename "$path")
  for protected in "${PROTECTED_FILES[@]}"; do
    if [ "$base" = "$protected" ] || [[ "$path" == *"$protected"* ]]; then
      printf '%s' "$base"
      return 0
    fi
  done
  return 1
}

INPUT=$(read_stdin)
EVENT=$(get_hook_event "$INPUT")

# ── Cursor dedicated event: commit-time staged-file check ───────────
if [ "$EVENT" = "beforeShellExecution" ]; then
  COMMAND=$(get_shell_command "$INPUT")
  [ -z "$COMMAND" ] && exit 0

  # Match anywhere so chained/prefixed forms are not bypassed
  # (e.g. `cd sub && git add`, `git -C path commit`).
  is_git_commit_or_add "$COMMAND" || exit 0

  PROJECT_ROOT=$(project_root_from_payload "$INPUT")

  STAGED=$(git -C "$PROJECT_ROOT" diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true)
  [ -z "$STAGED" ] && exit 0

  while IFS= read -r staged_path; do
    [ -z "$staged_path" ] && continue
    if name=$(matched_protected "$staged_path"); then
      deny "Protected config file is staged: $name ($staged_path).
This config file should not be modified by agents without explicit approval.
Unstage it (git restore --staged '$staged_path') or get approval before committing."
    fi
  done <<< "$STAGED"

  exit 0
fi

# ── Claude/Copilot: pre-write file-path check ───────────────────────
FILE_PATH=$(get_edit_file_path "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

if name=$(matched_protected "$FILE_PATH"); then
  deny "Protected file: $name. This config file should not be modified by agents without explicit approval."
fi

exit 0

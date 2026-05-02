#!/usr/bin/env bash
# config-protection — PreToolUse hook
# Blocks modification of linter, formatter, CI config, and lockfiles.
# Prevents agents from "fixing" project configuration without asking.
#
# Exit 2 = block
# Exit 0 = allow
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
FILE_PATH=$(get_file_path "$INPUT")

[ -z "$FILE_PATH" ] && exit 0

BASENAME=$(basename "$FILE_PATH")

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

for protected in "${PROTECTED_FILES[@]}"; do
  if [ "$BASENAME" = "$protected" ] || [[ "$FILE_PATH" == *"$protected"* ]]; then
    deny "Protected file: $BASENAME. This config file should not be modified by agents without explicit approval."
  fi
done

exit 0

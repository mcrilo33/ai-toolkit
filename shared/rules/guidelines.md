# Guidelines

## Agent Behavior

### Before Acting
- Verify file paths and imports exist before referencing them
- Check for existing patterns in similar files before creating new code
- When modifying existing code, read the file first to understand context
- Confirm understanding of ambiguous requirements before proceeding
- State your assumptions explicitly before acting on them

### Clarification
- If something is unclear, stop. Name what's confusing. Ask.
- If multiple interpretations exist, present them — don't pick silently
- If a simpler approach exists, say so. Push back when warranted.
- Prefer option-based clarification: propose a short bulleted list (2–5 items) of alternatives
- When a request could have unintended side effects, warn before proceeding

### Simplicity First
**Minimum that solves the problem. Nothing speculative.**
- Nothing beyond what was asked.

### Autonomy Threshold
- 1-2 files with clear scope → proceed without confirmation
- 3+ files, ambiguous scope, or destructive operations → confirm plan first

### Prohibitions
- Do not create new files unless explicitly requested or absolutely necessary
- Do not add new dependencies without asking
- Do not generate placeholder, stub, or incomplete implementations
- Do not modify code unrelated to the current task
- Do not remove or modify existing functionality unless explicitly asked
- Do not make assumptions about missing context — ask instead
- Do not hallucinate APIs, functions, or imports that may not exist

### Self-Correction
- If you realize a mistake mid-response, stop and correct immediately
- When a solution doesn't work, analyze why before trying alternatives
- Acknowledge uncertainty rather than guessing

## Response Style

### What to Do
- Be direct and concise; avoid unnecessary preamble
- Code first, explanation second (when explanation is needed)
- Working, complete code (not fragments unless asked)
- Specific file paths when referencing code
- State uncertainty explicitly: "I'm not sure if X, but..."
- Present 2-3 options maximum with brief trade-offs; recommend one

### What to Omit
- Excessive praise or validation ("Great question!")
- Restating the obvious or generic advice
- Long explanations when code is self-documenting
- Obvious or self-explanatory comments in code

## Security (Non-Negotiable)

### Secrets & Credentials (CRITICAL)
- **NEVER hardcode secrets, API keys, passwords, or credentials in code** — even temporarily
- When handling secrets, ALWAYS:
  1. Use environment variables in code
  2. Update code to read from `os.environ` or equivalent
- Never log or print sensitive data (tokens, passwords, PII)
- Before committing: verify no secrets are present in the diff

### Secrets Storage (macOS Keychain)

- Store all personal secrets in macOS Keychain via `security add-generic-password`
- Load in shell via `$(security find-generic-password -a "$USER" -s "KEY_NAME" -w 2>/dev/null)`
- Reference as env vars in configs: `${env:VAR_NAME}` (VS Code) / `${VAR_NAME}` (Cursor)
- Never write secrets to `.env`, `.zshrc`, or config files — even temporarily

### Input Validation
- Validate and sanitize all external inputs
- Use parameterized queries for database operations
- Escape user content before rendering in HTML/templates

### Dependencies
- Prefer well-maintained libraries with security track records
- Pin dependency versions in production
- Be cautious with `eval()`, `exec()`, or dynamic code execution

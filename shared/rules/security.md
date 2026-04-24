# Security (Non-Negotiable)

## Secrets & Credentials (CRITICAL)

- **NEVER hardcode secrets, API keys, passwords, or credentials in code** — even temporarily
- When handling secrets, ALWAYS:
  1. Use environment variables in code
  2. Update code to read from `os.environ` or equivalent
- Never log or print sensitive data (tokens, passwords, PII)
- Before committing: verify no secrets are present in the diff

## Secrets Storage

- All personal secrets are stored with macOS Keychain via `security add-generic-password`
- Load in shell via `$(security find-generic-password -a "$USER" -s "KEY_NAME" -w 2>/dev/null)`
- Reference as env vars in configs: `${env:VAR_NAME}` (VS Code) / `${VAR_NAME}` (Cursor)
- Never write secrets to `.env`, `.zshrc`, or config files — even temporarily

## Input Validation

- Validate and sanitize all external inputs
- Use parameterized queries for database operations
- Escape user content before rendering in HTML/templates

## Dependencies

- Prefer well-maintained libraries with security track records
- Pin dependency versions in production
- Be cautious with `eval()`, `exec()`, or dynamic code execution

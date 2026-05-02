# GitHub Actions conventions

## Workflow file

- Descriptive `name` for every workflow
- Use granular triggers with branch/path filters — avoid bare `on: push`
- Set `concurrency` for critical workflows to prevent parallel runs
- Set `permissions` at workflow level; default to `contents: read`
- Use reusable workflows (`workflow_call`) to reduce duplication

## Jobs

- One job per logical phase: `lint`, `test`, `build`, `deploy`
- Use `needs` to define dependencies between jobs
- Use `outputs` to pass data between jobs — avoid rebuilding
- Use `if` conditions for conditional execution (branch, event, prior status)
- Set `timeout-minutes` on long-running jobs

## Steps and actions

- **Pin actions to full commit SHA** with version comment:
  `uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2`
- Never use mutable refs (`@v4`, `@main`, `@latest`) — supply chain risk
- Give every step a descriptive `name`
- Audit third-party actions before use; prefer `actions/` org
- Use `dependabot` for action version updates

## Secrets

- Access via `${{ secrets.NAME }}` — never hardcode
- Use environment-specific secrets for deployment targets
- Never print or construct secrets dynamically in logs

## Permissions

- Explicit `permissions` block on every workflow
- Start with `contents: read`, add write only when needed
- Override per-job when a single job needs elevated access

## Caching

- Use `actions/cache` (SHA-pinned) for dependencies
- Key on lockfile hash: `hashFiles('**/package-lock.json')`
- Use `restore-keys` for fallback to older caches

## Checkout

- Default to `fetch-depth: 1` — full history only when needed
- Skip submodules and LFS unless required

## Testing

- Run tests early in the pipeline: `lint` → `unit` → `integration` → `E2E`
- Upload test reports as artifacts for visibility
- Use `services` for integration test dependencies (DB, cache)

## Matrix

- Use `strategy.matrix` for multi-version / multi-OS testing
- Set `fail-fast: false` for comprehensive reporting
- Use `include`/`exclude` to fine-tune combinations

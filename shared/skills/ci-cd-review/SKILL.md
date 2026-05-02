# CI/CD workflow review

Review, audit, or design GitHub Actions workflows against project standards.

## When to use

**Trigger phrases:**
- "Review this workflow"
- "Audit my CI/CD pipeline"
- "Is this workflow secure?"
- "Help me design a deployment pipeline"
- "Check my GitHub Actions config"

**Do NOT use when:**
- Making a small edit to a workflow file (the `github-actions` rule handles that)
- Writing application code that happens to be in a CI step

## Workflow

### 1. Identify review scope

Ask or infer which aspects matter:

- **Security** — secrets, OIDC, permissions, SCA/SAST, image signing
- **Optimization** — caching, matrix, artifacts, runners, checkout
- **Testing** — unit/integration/E2E/perf coverage in the pipeline
- **Deployment** — environments, approval gates, rollback, progressive delivery

### 2. Load relevant references

Read only the references needed for the scope:

- `references/security.md`
- `references/optimization.md`
- `references/testing.md`
- `references/deployment.md`

### 3. Audit against checklist

Use `references/checklist.md` for a comprehensive pass across all areas.

### 4. Report findings

Structure output as:

- **Critical** — security issues, missing SHA pinning, exposed secrets
- **Recommended** — optimization gaps, missing caching, broad permissions
- **Optional** — nice-to-haves, advanced patterns

Include specific fix suggestions with code snippets.

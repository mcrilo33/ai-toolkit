# CI/CD optimization reference

## Caching

- Use `actions/cache` (SHA-pinned) for package manager dependencies and build outputs
- Design cache keys with `hashFiles` for optimal hit rates
- Use `restore-keys` for fallback to older, compatible caches
- Caches are scoped to the repository and branch

```yaml
- name: Cache Node.js modules
  uses: actions/cache@668228422ae6a00e4ad889ee87cd7109ec5666a7 # v5.0.4
  with:
    path: |
      ~/.npm
      ./node_modules
    key: ${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}
    restore-keys: |
      ${{ runner.os }}-node-
```

## Matrix strategies

- Use `strategy.matrix` for multi-version / multi-OS testing
- Use `include`/`exclude` to fine-tune combinations
- `fail-fast: false` for comprehensive reporting; `fail-fast: true` for quick feedback

```yaml
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        node-version: [18.x, 20.x, 22.x]
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      - uses: actions/setup-node@3235b876344d2a9aa001b8d1453c930bba69e610 # v3.9.1
        with:
          node-version: ${{ matrix.node-version }}
      - run: npm ci && npm test
```

## Self-hosted runners

- Use when GitHub-hosted runners lack performance, cost, or network access requirements
- Secure and maintain your own infrastructure — hardening, patching, access controls
- Use runner groups for organization; plan for auto-scaling

## Fast checkout and shallow clones

- Default to `fetch-depth: 1` for most build and test jobs
- Full history (`fetch-depth: 0`) only for release tagging, git blame, or changelog
- Skip submodules (`submodules: false`) and LFS (`lfs: false`) unless required
- Consider partial clones (`--filter=blob:none`) for very large repositories

## Artifacts

- Use `actions/upload-artifact` / `actions/download-artifact` (SHA-pinned) for inter-job data
- Set `retention-days` to manage storage costs
- Upload test reports, coverage, and security scan results as artifacts
- Pass compiled binaries from build to deploy jobs — don't rebuild

## General tips

- Set `timeout-minutes` on all jobs to prevent hung workflows
- Combine `run` commands with `&&` to reduce overhead
- Install only necessary dependencies
- Break large workflows into smaller, reusable workflows

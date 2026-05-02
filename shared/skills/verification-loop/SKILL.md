# Verification Loop

Run a structured quality-gate pipeline and produce a pass/fail report before closing a task.

## When to Use

- Before committing or creating a PR
- After finishing implementation (EXECUTE → VERIFY transition)
- When the user says "verify", "check everything", "run quality gates", or "pre-flight"
- Invoked automatically by the `close-task` skill if verification hasn't been run

## Gates

Run each gate in order. Stop early on critical failures (build, tests) unless the user asks to continue.

### 1. Build

Detect the project's build command and run it:

| Indicator | Command |
|-----------|---------|
| `Makefile` | `make build` |
| `pyproject.toml` | `python -m build` or `pip install -e .` |
| `package.json` (`build` script) | `npm run build` / `pnpm build` / `yarn build` |
| `Cargo.toml` | `cargo build` |
| `go.mod` | `go build ./...` |

If no build step applies, mark as `SKIP` (not `FAIL`).

### 2. Type Check

| Indicator | Command |
|-----------|---------|
| `tsconfig.json` | `npx tsc --noEmit` |
| `pyproject.toml` with mypy/pyright | `mypy .` or `pyright` |
| `go.mod` | `go vet ./...` |
| `Cargo.toml` | (covered by build) |

If no type checker is configured, mark as `SKIP`.

### 3. Lint

| Indicator | Command |
|-----------|---------|
| `.eslintrc*` / `eslint.config.*` | `npx eslint .` |
| `biome.json` | `npx biome check .` |
| `ruff.toml` / `pyproject.toml` with ruff | `ruff check .` |
| `pylintrc` / `.pylintrc` | `pylint <package>` |
| `.golangci.yml` | `golangci-lint run` |
| `clippy` (Rust) | `cargo clippy -- -D warnings` |

If no linter is configured, mark as `SKIP`.

### 4. Tests

Detect and run the test suite:

| Indicator | Command |
|-----------|---------|
| `pytest.ini` / `pyproject.toml` with pytest | `pytest` |
| `package.json` (`test` script) | `npm test` / `pnpm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

Report: total tests, passed, failed, skipped.

### 5. Security Scan

Quick checks (no external tools required):

```bash
# Hardcoded secrets — fail if any match
git diff --cached --diff-filter=ACM | grep -inE '(sk-|ghp_|AKIA|password\s*=\s*["\x27][^"\x27]+|secret\s*=\s*["\x27][^"\x27]+)'

# .env files staged — warn
git diff --cached --name-only | grep -E '\.env($|\.)'
```

If a project-specific scanner exists (e.g., `bandit`, `npm audit`, `cargo audit`), run it too.

### 6. Diff Review

Analyze the current diff for quality issues:

```bash
git diff --stat
git diff --cached --stat
```

Check for:

- **Unintended files** — binary files, lock files, IDE configs staged by mistake
- **Large diff** — warn if >400 lines changed (suggest splitting)
- **Debug artifacts** — `console.log`, `print(`, `debugger`, `TODO`, `FIXME` left behind
- **Commented-out code** — blocks of commented code that should be removed

## Output Format

Produce a summary table after all gates run:

```text
┌─────────────────┬────────┬─────────────────────────────────┐
│ Gate            │ Result │ Details                         │
├─────────────────┼────────┼─────────────────────────────────┤
│ Build           │ ✅ PASS │                                 │
│ Type Check      │ ⬚ SKIP │ No type checker configured      │
│ Lint            │ ✅ PASS │ 0 errors, 2 warnings            │
│ Tests           │ ✅ PASS │ 42 passed, 0 failed, 1 skipped  │
│ Security Scan   │ ✅ PASS │ No secrets detected             │
│ Diff Review     │ ⚠ WARN │ 1 console.log found             │
├─────────────────┼────────┼─────────────────────────────────┤
│ Overall         │ ✅ PASS │ Ready to close                  │
└─────────────────┴────────┴─────────────────────────────────┘
```

Result values:

| Symbol | Meaning |
|--------|---------|
| ✅ PASS | Gate succeeded |
| ❌ FAIL | Gate failed — must fix before closing |
| ⚠ WARN | Non-blocking issue — review before closing |
| ⬚ SKIP | Gate not applicable to this project |

## Overall Verdict

- **PASS** — all gates are PASS or SKIP
- **WARN** — all critical gates pass, but warnings exist
- **FAIL** — any gate is FAIL

## Behavior

- Run gates sequentially (each may depend on prior success)
- On FAIL in Build or Tests: stop and report (remaining gates won't be meaningful)
- On FAIL in Lint or Security: continue remaining gates, then report all
- On WARN: continue, include in final report
- Present the summary table and ask the user how to proceed if not all PASS

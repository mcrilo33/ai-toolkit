# Debug Mode — Find and Fix Bugs

Systematically identify, analyze, and resolve bugs. Reproduce first, understand second, fix last.

## Scope Boundary

**You ONLY fix the reported bug. NEVER refactor, add features, or "improve" unrelated code.**

If you spot other issues, note them — do not fix them.

## Phase 1: Triage

Classify the bug to pick the right investigation path:

| Category | Signals | First move |
| -------- | ------- | ---------- |
| **Test failure** | `FAILED` in test output, `AssertionError` | Read the failing test and assertion message |
| **Runtime crash** | Stack trace, exception | Read the stack trace bottom-up, open the crash site |
| **Silent wrong behavior** | No error but wrong output | Add assertions or logging at key checkpoints |
| **Build/import error** | `ModuleNotFoundError`, `SyntaxError` | Check imports, dependencies, Python version |

## Phase 2: Reproduce

Before making ANY changes:

1. **Run the failing test or reproduction command** — confirm the bug exists now.
2. **Capture the exact error output** — save it for comparison after fix.
3. **If it can't be reproduced** — stop. Report this to the user. Do not guess.

## Phase 3: Investigate

### Trace the execution path

1. **Start at the error site** — read the file and function where the failure occurs.
2. **Trace backwards** — follow the call chain from error to entry point.
3. **Check recent changes** — use `git log` / `git diff` to find what changed.
4. **Search for related code** — find other callers/usages of the broken function.

### Form and test hypotheses

1. **State your hypothesis explicitly** — "I think X fails because Y."
2. **Verify with the simplest check** — read code, add a print, run a targeted test.
3. **If wrong, discard and try next** — don't force-fit a hypothesis.
4. **Limit to 3 attempts** — if three hypotheses fail, report findings and ask for guidance.

### Common root causes to check

- Wrong variable/argument (typo, wrong order, stale reference)
- Missing edge case (None, empty, zero, boundary values)
- State mutation (shared mutable state, missing copy)
- Incorrect assumption about API/library behavior
- Race condition or ordering issue
- Recent regression from a nearby change

## Phase 4: Fix

1. **Make the smallest change that fixes the root cause** — one targeted edit.
2. **Follow existing code patterns and conventions** — match the surrounding style.
3. **Run the original failing test** — confirm it passes.
4. **Run the full test suite** — confirm no regressions.
5. **If the fix breaks other tests** — the fix is wrong. Revert and revisit Phase 3.

## Phase 5: Verify and Report

1. **Re-run the original reproduction steps** — confirm the bug is resolved end-to-end.
2. **Add a regression test if none exists** — prevent this bug from returning.
3. **Summarize concisely:**
   - **Root cause:** one sentence explaining why it broke.
   - **Fix:** one sentence explaining what you changed.
   - **Files changed:** list of modified files.
   - **Risk:** any potential side effects (or "none" if straightforward).

## Iteration Loop

Debugging is not linear. If a fix doesn't work:

```text
Phase 3 (investigate) → Phase 4 (fix) → Phase 4 fails → back to Phase 3
```

After 3 failed fix attempts, stop and present your findings to the user.

## Guidelines

- **Reproduce before you touch code** — no exceptions.
- **One hypothesis at a time** — don't shotgun multiple changes.
- **Minimal changes** — fix the bug, nothing else.
- **Stay in scope** — flag unrelated issues, don't fix them.
- **Read before you write** — understand the code before changing it.
- **Revert on regression** — if your fix breaks something, undo it immediately.
- **Report clearly** — the user should understand what happened without reading the diff.

## Checklist

- [ ] Bug reproduced and error output captured
- [ ] Root cause identified (not just symptoms)
- [ ] Fix is minimal and targeted
- [ ] Original failing test passes
- [ ] Full test suite passes (no regressions)
- [ ] Regression test added (if applicable)
- [ ] Summary reported to user

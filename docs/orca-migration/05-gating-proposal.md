# 05 — Proposal: where "it is tested" lives (gating model)

Date: 2026-10-02. Status: **decided 2026-10-02: option B, now** (see §7).
Context: the first day of running the Orca migration with parallel workers (STATUS.md, epic #377).

## 1. Problem: evidence from 2026-10-02

The workers wrote the code of six issues in a few hours. Shipping it took the rest of the day.

| Observation | Data |
|---|---|
| Local push gate duration | 5 min (mapped tier) to **33 min** (#371's land: hub venv had no `pytest-xdist`, full suite serial) |
| Gates per branch | push gate + often a second full gate at land (non-fast-forward merge) |
| Push failures unrelated to the diff | #371 ×3, #361 ×2, #363 ×1. Causes: pre-existing flakes on main (#374, #375), machine overload |
| Machine load | 10–21 with 4–6 agents + 2 coordinators + concurrent suites; timing-sensitive tests break |
| Flake root cause found | macOS cold first-exec of fresh stub scripts (1–3 s) vs 2–4 s bounded waits; wall-clock assertions |
| Coordination | hand-made serialization (`ship.sh`, `push-queue.sh`), two coordinators (hub session + migration session) sharing one Mac |
| Merge conflicts | hot files touched by almost every issue: `scripts/sync-to-repo.sh`, `tests/unit/test_sync_workflow_scripts.py`, `hub-afk*.sh` |
| CI today | `.github/workflows/ci.yml` runs the full suite on **ubuntu-latest in ~7 min** (serial), ShellCheck, sync idempotency, a macOS fr_FR subset (currently failing in 12 s, separate issue). It runs on push to `main` and PRs, i.e. **after** landing. The repo is **public**, so Actions minutes are free. |

**Diagnosis:** the local Mac does three jobs at once: run agents, run every gate, and be the source of truth for "tested". Each extra worker makes gates slower and flakier, so parallelism defeats itself.

## 2. Goals

1. A worker's branch reaches `main` in **minutes after it is done**, not hours.
2. "Tested" means the same thing for every branch, independent of how loaded the Mac is.
3. A flake on `main` blocks **nobody's** diff while it is being fixed (it gets fixed, not ignored).
4. Less code and fewer moving parts in ai-toolkit (the migration goal).
5. Keep a fast local safety net (a broken commit should not even leave the machine).

## 3. Options

### A — Status quo + fixes
Keep the local full gates; fix flakes (#376, #377), install xdist everywhere, serialize gates in the S6 coordinator.
- **+** No policy change.
- **−** Throughput stays bound by one Mac running all suites. Gates still collide with agents. Flakes keep blocking until each is found.

### B — CI is the gate; local runs only the fast tier (recommended)
- **Local pre-push:** the mapped/selected tests only (seconds to 1–2 min) + lint/typecheck. **No full suite locally.**
- **Every branch push triggers CI** (extend `ci.yml` `on.push.branches` to `**`, or the spoke branch pattern). The full suite runs on Ubuntu, with `-n auto`.
- **The `ready/<n>` marker requires a green CI run for that exact SHA.** `spoke-ready.sh` waits on, or checks, `gh run` status. Land = fast-forward or merge, then CI on `main` (already there).
- **+** The Mac only runs agents; gates run in parallel on GitHub, for free (public repo). Linux removes the macOS cold-exec flake class. Branches no longer wait for each other.
- **−** Depends on network + GitHub. A flake in CI still blocks, but no longer on your machine. Shell tests sensitive to macOS (fr_FR locale, BSD tools) need the macOS job fixed and kept as a required check.

### C — B + batch landing (merge queue)
Land several `ready` branches together: an integration branch merges them in order, CI runs **once** for the batch, then fast-forwards `main`. If red, bisect.
- **+** One CI run per batch instead of one per branch. Conflicts are resolved once, in order.
- **−** More machinery: either GitHub's merge queue (needs PRs + branch protection; the current flow is PR-less) or a scripted queue in the coordinator. Worth it only at more than ~5 lands/day.

### D — Remote runners for gates (Orca runtime / VM, plan S10)
Run gates on an Orca remote runtime or VM instead of the Mac.
- **+** Keeps everything "local-first".
- **−** Still single-machine, more infrastructure; CI already does this for free.

## 4. Comparison

| | A | B (rec.) | C | D |
|---|---|---|---|---|
| Time from "done" to `main` | 20–60 min, serialized | ~10 min (CI), parallel | ~10 min per batch | ~10–20 min |
| Effect of Mac load on gates | high | none | none | low |
| macOS cold-exec flake class | must be fixed | gone in the gate (Linux) | gone | depends |
| New code in ai-toolkit | ~0 | small (wait-for-CI in `spoke-ready.sh`, `ci.yml` trigger) | medium (queue) | high |
| Code removed | 0 | local full-suite tiers of `test-select.sh`, green stamps, tripwire scoping (to measure) | same as B | 0 |
| Works offline | yes | push yes, `ready` no | no | no |
| Policy change | none | **yes** | yes | partial |

## 5. Recommended path

1. **Now (with the migration):** keep A's fixes already running (#375, #376, xdist in venvs).
2. **Adopt B right after S6 (#365):**
   - `ci.yml`: run on every branch push, with `pytest -n auto`, and **fix the macOS job** so it is a meaningful required check;
   - `test-select.sh`: drop the local full-suite tiers. Local = mapped tests + meta-tests only, plus the anti-gutting scan;
   - `spoke-ready.sh ready <n>` refuses until `gh run` reports success for HEAD (with a clear message and a timeout);
   - `worktree-land.sh`: no local suite. A fast-forward of a CI-green SHA is landed directly; a merge commit waits for CI on the merged SHA (or rebase-then-FF);
   - the Orca coordinator (S6) no longer serializes gates. It only serializes lands.
3. **Revisit C** if lands exceed ~5/day for a sustained period.
4. **Hot-file conflicts:** make sync registration declarative (each script declares itself, e.g. a header tag or a per-file manifest) instead of the central name list in `sync-to-repo.sh`. This is a separate issue in epic #377.

### 5.1 Merge queue availability (tested 2026-10-02)

- Repo owner is a **personal account** (`owner.type=User`), repo public, no rulesets or branch protection on `main`.
- Reversible probe: `POST /repos/mcrilo33/ai-toolkit/rulesets` with `enforcement: disabled` and a `merge_queue` rule →
  **HTTP 422 `Invalid rule 'merge_queue'`**. Control: the same disabled ruleset with a `non_fast_forward` rule → **created**
  (id 24365498) and deleted immediately. Rulesets list is empty again.
- Conclusion: **GitHub's native merge queue is not available on this repo** (consistent with "organization-owned repositories
  only"). Native C would need moving the repo to a (free) GitHub organization. Otherwise C means a scripted queue in the
  Orca coordinator (~100–150 added lines).
- Implication: **B now** (each branch re-runs CI on the up-to-date main just before land; CI runs are parallel and free),
  and either a scripted batch queue later if volume requires it, or an org transfer for native C.

## 6. Decisions for the user

Tick one per line, or amend.

- **D1. Gating model:** ☐ A (status quo + fixes) ☐ **B (CI gate, local fast tier)** ☐ C (B + merge queue now) ☐ D
- **D2. Offline work:** with B, `ready` needs network/CI. ☐ acceptable ☐ need an offline fallback (`--local-gate` running the full suite locally on demand)
- **D3. macOS coverage:** ☐ fix the macOS CI job and make it required ☐ Linux-only CI, macOS checked locally on demand
- **D4. Timing:** ☐ after S6 (recommended; S6 rewrites the coordinator anyway) ☐ now, before finishing the migration
- **D6. Native merge queue:** ☐ transfer the repo to a free GitHub organization to enable it (then C natively, PR-based)
  ☐ stay on the personal account (B; scripted batching only if needed)
- **D5. Declarative sync registration** (removes the hot-file conflicts): ☐ yes, add to epic #377 ☐ no

On validation, the migration session files the corresponding issue(s), Orca-only replace-and-delete style, with net-negative LOC targets where applicable.

## 7. Decision record (2026-10-02)

- **D1 = B**, **D4 = now** (user). Defaults taken for the rest (recommendations): **D2** offline escape hatch `--local-gate`;
  **D3** fix the macOS CI job and make it required; **D5** declarative sync registration added to epic #377; **D6** stay on the
  personal account (no native merge queue).
- Issues: **#378** (option B: CI gate, local fast tier, ready/land wait for green CI; Orca worker `ctx_92a6a2069701`),
  **#379** (macOS fr_FR CI job fix; Orca worker `ctx_7ea1f32f2c12`).

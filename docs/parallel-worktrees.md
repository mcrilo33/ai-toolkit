# Parallel worktrees workflow

Run several Claude Code sessions at once — one git worktree per task — so concurrent
edits never collide, while reviewing everything from a single VS Code window. The
`scripts/worktree-*.sh` helpers automate creation, config propagation, and teardown.

## The model

One invariant makes the whole flow clean: **the main checkout always stays on `main`
and never holds task work.** It is your launcher and merge hub. Every task lives in its
own worktree, on its own branch, driven by its own Claude in its own tmux window.

```
~/Repos/ai-toolkit          MAIN CHECKOUT — always on `main`. Launch + merge here.
│
├── one VS Code window  ◄──── `code --add` folds every worktree in (your review surface)
│
├─ ai-toolkit-42  feature/42-…  ◄─ tmux window "42-…" → claude   ┐
├─ ai-toolkit-57  feature/57-…  ◄─ tmux window "57-…" → claude   │ N isolated tasks
└─ ai-toolkit-63  feature/63-…  ◄─ tmux window "63-…" → claude   ┘
```

| Concept | Maps to |
|---------|---------|
| One task | one issue → one branch → one worktree dir → one tmux window → one Claude |
| Isolation unit | a git worktree (separate working tree, staging area, `.claude/` gates) |
| Review surface | a single VS Code window, one Source Control group per worktree |
| Merge hub | the main checkout, kept on `main` |

## Planning hub and execution spokes

Organize your Claude sessions as one long-lived **planning hub** and many short-lived
**execution spokes**. The hub is a persistent session on the main checkout where you
think, decide, and decompose; it stays read-only on the repo and its output is GitHub
issues. Each spoke is a worktree session that implements one issue under the gates.

```
 PLANNING HUB  (main checkout, one persistent session)   think → decide → write issue
        │  hands off via an issue + start-task
        ▼
 worktree-new.sh N → SPOKE N  (worktree + tmux window + seeded claude)  /source → /cycle
```

| Aspect | Planning hub | Execution spoke |
| ------ | ------------ | --------------- |
| Lives on | the main checkout (`main`) | its own worktree + branch |
| Lifespan | long — reused across tasks | short — created, worked, merged, destroyed |
| Touches code | no — explores and decides | yes — gated RED / GREEN / REVIEW / PUSH |
| Produces | GitHub issues + the launch command | commits on the task branch |

The **issue is the contract** between hub and spoke: the spoke starts with a fresh,
focused context containing just that issue, so planning noise never leaks in. When scope
is clear, the `start-task` skill does the handoff in one step — it drafts and creates the
issue, then runs `worktree-new.sh <id> --prompt …` to spawn the spoke and seed its first
message. The `source-task` guard nudges you back to this split if you start coding on the
hub.

## One-time setup

```bash
# 1. Open your single review window — keep just this ONE VS Code window open
code ~/Repos/ai-toolkit

# 2. Be in a tmux session in the main-checkout terminal (see "tmux" below)
```

> [!NOTE]
> The helper scripts must be present on `main` so that every worktree branched from it
> carries them. They live under `scripts/`; if a worktree reports `no such file`, the
> branch predates the merge — run the script from the main checkout by absolute path,
> or merge the scripts into `main`.

## The scripts

| Script | Role |
|--------|------|
| `scripts/worktree-new.sh` | Create a worktree + branch, copy `.claude/`, fold into VS Code, open a tmux window running `claude` |
| `scripts/worktree-done.sh` | Resolve a worktree by issue / slug / branch / path and tear it down safely |
| `scripts/worktree-lib.sh` | Shared slugify + main-root + worktree-resolution helpers (sourced by both) |

## The daily loop

### 1. Start a task

```bash
# from the main-checkout terminal
scripts/worktree-new.sh 42              # <id> = GitHub issue number (slug from the title)
scripts/worktree-new.sh fix-parser      # ad-hoc: the first arg IS the slug → feature/fix-parser
```

Each run automatically:

1. creates `~/Repos/ai-toolkit-<tag>` on branch `feature/<id>-<slug>`,
2. copies the gitignored `.claude/` runtime config (skills + hooks + gates) into it,
3. runs `code --add` to fold the worktree into your single VS Code window,
4. opens a new tmux window in session `0` (the spoke home), named after the branch
   leaf (e.g. `42-fix-crash`), pinned against renames, running `claude` in the
   worktree — and prints the exact jump command to reach it.

Paste the printed jump command (or switch with `prefix` + number inside session `0`)
and drive Claude — typically `/source` then `/cycle`.

### 2. Work and review

Each session is fully isolated: its own files, branch, staging area, and `.claude/`
gates. In the one VS Code window, the Source Control panel shows **one group per
worktree**, so you review every task's diff without leaving the window.

### 3. Merge and tear down

When a task's branch is committed and pushed:

```bash
cd ~/Repos/ai-toolkit            # merge hub, already on main
git merge feature/42-42           # land it (fast-forward when possible)
git push origin main              # ship

scripts/worktree-done.sh 42       # remove the worktree, fold its folder out of
                                  # VS Code, and prune the now-merged branch
                                  # (local + origin) — all automatic
```

Teardown is the mirror of creation: it runs `code --remove` to drop the folder
from the review window, and because the branch is now merged into the hub it
deletes both `feature/42-42` and `origin/feature/42-42` for you. An unmerged
branch is kept untouched (with a push/merge-first hint); pass `--keep-branch` to
keep a merged one, or `--no-code` to leave VS Code alone.

## `worktree-new.sh` reference

```
scripts/worktree-new.sh <issue> [slug] [type] [flags]
```

| Argument | Meaning |
|----------|---------|
| `<issue>` | GitHub issue number (slug derived from the issue title via `gh`), or a bare slug for ad-hoc work |
| `[slug]` | explicit branch slug; always slugified, so spaces/odd characters can't break the ref |
| `[type]` | `feature` (default), `fix`, or `chore` |

| Flag | Effect |
|------|--------|
| `-t, --type <t>` | branch type (`feature`/`fix`/`chore`) — unambiguous, beats the positional `[type]` slot |
| `--prompt <text>` | seed the spawned `claude` with this first message (e.g. `/source` or a task kickoff) |
| `--new-window` | open a separate VS Code window instead of `code --add` |
| `--no-code` | do not touch VS Code |
| `--no-terminal` | do not spawn a tmux/terminal window |
| `--no-agent` | spawn the terminal but do not launch `claude` |

The spawned agent's model and effort are pinned at dispatch time
(`CLAUDE_EFFORT=max claude --model fable` by default) so a spoke stays
deterministic even when user-global settings change; override with the
`WT_AGENT_MODEL` / `WT_AGENT_EFFORT` env vars.

Branch naming: `feature/<id>-<slug>` for numeric issues, `<type>/<slug>` for ad-hoc.
This convention matches the `source-task` and `solo-cycle` skills and the
`commit-quality` issue-anchor gate.

Names are normalized: lowercased, every run of non-`[a-z0-9]` becomes a single `-`,
edges trimmed, and only the first four hyphen-separated segments are kept (so
`Refactor_The_Whole_Sync_Engine` → `refactor-the-whole-sync`). Non-ASCII letters are
dropped. `[type]` must be `feature` (default), `fix`, or `chore`.

> [!WARNING]
> Arguments are positional as `<issue> [slug] [type]`, so `[type]` is the **third** one.
> For ad-hoc work the first argument is already the slug, so
> `worktree-new.sh fix-parser fix` makes `fix` the *slug* (branch `feature/fix`) and
> discards `fix-parser` — it does **not** set the type. To set a type on ad-hoc work,
> pass an empty slug: `worktree-new.sh fix-parser "" fix` → `fix/fix-parser`.

## `worktree-done.sh` reference

```
scripts/worktree-done.sh <issue|slug|branch|path> [--force] [--no-code] [--keep-branch]
```

Resolves the target against the live `git worktree list` — by issue number, slug,
branch name, or path — and removes it. On no match or an ambiguous match it **lists the
existing worktrees** instead of failing with a dead-end error.

Teardown then mirrors `worktree-new.sh`: it folds the folder out of the VS Code review
window (`code --remove`) and prunes the worktree's branch. The branch is pruned **only
when it is fully merged** into the hub's current branch — local *and* `origin/<branch>`
are deleted automatically. An unmerged branch is kept untouched, with a push/merge-first
hint. `code` and remote failures warn but never abort: the worktree removal still
succeeds.

| Flag | Effect |
|------|--------|
| `--force` | remove a worktree with uncommitted or untracked changes |
| `--no-code` | don't fold the folder out of VS Code (`code --remove`) |
| `--keep-branch` | keep the branch even when it is fully merged |

All three flags are position-independent.

## tmux

All spokes live as windows of tmux session `0` — the **spoke home**. The script
targets it explicitly (creating it detached when missing), regardless of which tmux
session — or none — you invoke it from, so a spoke can never land in a stray
per-task session with no client attached. Each run prints the exact jump command:
`tmux switch-client -t '0:<window>'` from inside tmux, or
`tmux attach -t 0 \; select-window -t '0:<window>'` from a plain shell. Inside
session `0`, switch between task windows with `prefix` + number.

## Notes and gotchas

- **`.claude/` is copied, not symlinked.** A plain `git worktree add` checks out only
  tracked files, and `.claude/` is gitignored, so the script copies it (excluding
  `.review/`, `worktrees/`, and `*.bak`). `.review/` must start empty per checkout, or a
  push could pass on another worktree's approval.
- **`.worktreeinclude` does not apply here.** Claude Code's native `.worktreeinclude`
  only runs for native `claude -w` worktrees, not the `git worktree add` these scripts
  use — hence the explicit copy.
- **`code --add` targets the last-active VS Code window.** Keep a single review window
  open so new worktrees always fold into the right place.
- **Window, not pane.** The script opens a tmux *window* per task (switch with `prefix`
  + number), not a split pane.
- **Removal needs `--force` only when dirty.** Because `.claude/` is gitignored, a copied
  config does not make the worktree dirty, so a clean task removes without `--force`.

## Related

- `shared/skills/source-task/SKILL.md` — anchors a task to an issue; warns when you run
  `/source` on the shared main checkout instead of a worktree
- `shared/skills/solo-cycle/SKILL.md` — the per-subtask RED / GREEN / REVIEW / PUSH cycle
- `docs/metadata-and-sync.md` — how `shared/` propagates into the live `.claude/` config

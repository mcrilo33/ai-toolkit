# Parallel worktrees workflow

Run several Claude Code sessions at once — one git worktree per task — so concurrent
edits never collide, while reviewing everything from a single VS Code window. The
`scripts/worktree-*.sh` helpers automate creation, config propagation, and teardown.

## The model

One invariant makes the whole flow clean: **the main checkout always stays on `main`
and never holds task work.** It is your launcher and merge hub. Every task lives in its
own worktree, on its own branch, driven by its own Claude in its own tmux window.

The mirror invariant governs pushes: **a spoke pushes only its own branch** — its
`origin/<branch>` is ephemeral staging, deleted at teardown once merged — and **`main`
is published exclusively from the hub**. The `push-scope-guard` hook enforces both
directions (hard deny on Cursor, advisory elsewhere).

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
> The skills and the `planning-hub` rule invoke the helpers at the canonical
> `.ai-toolkit/scripts/` path so the same reference resolves in this checkout and in any
> synced target. `sync-to-repo.sh` installs them there (see
> [Installing into another repo](#installing-the-workflow-into-another-repo)); the
> ai-toolkit checkout gets them by syncing into itself. The hub commands run on the main
> checkout, where `.ai-toolkit/scripts/` lives — spokes never invoke them directly.

## The scripts

| Script | Role |
|--------|------|
| `scripts/worktree-new.sh` | Create a worktree + branch, copy `.claude/`, fold into VS Code, open a tmux window running `claude` |
| `scripts/worktree-land.sh` | Land a pushed branch from the hub: guards → merge → suite → push → teardown → issue close |
| `scripts/worktree-done.sh` | Resolve a worktree by issue / slug / branch / path and tear it down safely |
| `scripts/worktree-lib.sh` | Shared slugify + main-root + worktree-resolution helpers (sourced by the others) |

The paths above are the **source** locations in this repo. In a synced target the same
four scripts (plus `hub-status.sh`) live under `.ai-toolkit/scripts/` — see below.

## Installing the workflow into another repo

The hub/spoke/land flow is not specific to the ai-toolkit checkout: `sync-to-repo.sh`
installs it into **any** repo, with no manual steps afterwards.

```bash
./scripts/sync-to-repo.sh ~/Repos/my-project
```

This copies `worktree-{new,land,done,lib}.sh` and `hub-status.sh` into
`my-project/.ai-toolkit/scripts/` (executable, manifest-tracked, consistent with the
`.ai-toolkit/mcp/` convention). The `hub`, `start-task`, and `land` skills and the
`planning-hub` rule all invoke the scripts via that canonical `.ai-toolkit/scripts/`
path, which resolves identically in the ai-toolkit checkout and in the synced target —
so no sync-time path rewrite is needed.

The scripts locate the main checkout by git introspection (`wt_main_root` via
`git worktree list`) and source their siblings by their own directory, so they run
unmodified from `.ai-toolkit/scripts/` in a foreign repo. After a sync, run `/hub` in
`my-project` and the dashboard, dispatch, and land commands work there directly.

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
3. seeds `.claude/settings.local.json` with the two narrow allow rules for the
   spoke's own-branch push (`git push [-u] origin <branch>`) — the ship push is
   enforced by the push gates, so it skips the permission ask; every other push
   stays gated,
4. runs `code --add` to fold the worktree into your single VS Code window,
5. opens a new tmux window in the project's session (named after the repo — see
   [tmux](#tmux)), named after the branch leaf (e.g. `42-fix-crash`), pinned against
   renames, running `claude` in the worktree — and prints the exact jump command.

Paste the printed jump command (or switch with `prefix` + number inside the project
session) and drive Claude — typically `/source` then `/cycle`.

### 2. Work and review

Each session is fully isolated: its own files, branch, staging area, and `.claude/`
gates. In the one VS Code window, the Source Control panel shows **one group per
worktree**, so you review every task's diff without leaving the window.

### 3. Land

When a task's branch is committed, pushed, **and signalled complete**, land it from the
hub — `/land 42` in the hub session (the `land` skill), or directly:

```bash
cd ~/Repos/ai-toolkit            # merge hub, already on main
scripts/worktree-land.sh 42      # guards → merge (ff when possible) → full suite →
                                 # push origin main → teardown → close issue #42
```

Completion is explicit: a per-subtask push is indistinguishable from a finished issue,
so a numbered branch must carry a `ready/<issue>` git tag at its tip — the marker the
spoke emits after its FINAL subtask's push via the canonical emitter
(`bash .ai-toolkit/scripts/spoke-ready.sh 42`, also reached by `spoke-push.sh --ready
42`). Without it the land refuses (the branch reads `pushed (in progress)` on the
dashboard); pass `--force-land` only for an express/ad-hoc branch that never carries
one. A successful land consumes the marker (deletes the local + remote tag).

One command runs the whole landing: it refuses with a precise reason unless the hub is
clean on `main` and the spoke is clean, fully pushed, and marked ready; a failing suite
rolls `main` back (`git reset --keep`) with nothing pushed. On success it calls
`worktree-done.sh` for the teardown mirror of creation — `code --remove` drops the
folder from the review window, and the now-merged branch is pruned local + origin
(`--keep-branch` to keep it). An unmerged branch is never pruned. It then closes the
issue via `gh` and kills the task's stranded tmux window.

Landing is hub-owned by **role**, not directory. `worktree-new.sh` stamps each spoke
session with a `WT_SPOKE=<issue-or-slug>` env var that rides every command it runs, so
even a spoke that `cd`s into the main checkout is refused by `worktree-land.sh` and
`worktree-done.sh` before any work — there is no override flag. A spoke that is finished
emits its `ready/<issue>` marker and hands off; the hub (started directly by you, never
carrying `WT_SPOKE`) does the merge and teardown.

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

Each project gets **one tmux session**, and every spoke lives as a window of it.
The session name is derived from the repo root — the parent directory plus the repo
basename (`<parent>-<base>`, e.g. `Repos-ai-toolkit`), with tmux's forbidden `.` and
`:` mapped to `-`. Two checkouts that share a basename under different parents
therefore get distinct sessions, and `tmux ls` reads as a per-project portfolio
with each project's spokes nested under it. The script targets the session
explicitly (creating it detached when missing), regardless of which tmux session —
or none — you invoke it from, so a spoke can never land in a stray per-task session
with no client attached. Each run prints the exact jump command:
`tmux switch-client -t '<sess>:<window>'` from inside tmux, or
`tmux attach -t '<sess>' \; select-window -t '<sess>:<window>'` from a plain shell.
Inside a project session, switch between task windows with `prefix` + number.

The agent launch (pinned model/effort plus the seeded `--prompt`) is passed to
`new-window` as the window's own shell command — never typed into an interactive
shell, where `send-keys` raced zsh init and lost the trailing Enter (issue #15).
The command ends with `; exec $SHELL`, so the window drops back to a shell prompt
instead of closing when claude exits.

To re-seed a prompt into an **existing** pane manually (e.g. a claude sitting at
its input without the kickoff), send the text literally and the Enter separately:

```bash
tmux send-keys -t '<sess>:<window>' -l '/source'   # -l = literal text, no key-name parsing
tmux send-keys -t '<sess>:<window>' Enter          # separate Enter, not a trailing C-m
```

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

## Task triage — three lanes

Every task is classified on the hub (~10 seconds) before any worktree or issue is created:

| Lane | Executor | Contract | Gates |
|------|----------|----------|-------|
| 1 — Micro-spoke | Subagent in a temp worktree (no issue, no tmux window, no session) | The prompt | Hub diff-review before merge |
| 2 — Express spoke | Full spoke via `worktree-new.sh <slug>` (ad-hoc, no issue) | Kickoff prompt | All push gates, single cycle, no ledger |
| 3 — Full | Full spoke from a GitHub issue | The issue | Everything (current flow) |
| Express-interactive (`/quick`) | The hub session itself, in a `worktree-quick.sh` worktree — no separate session | The conversation | All push gates; no issue, ledger, PLAN, RED, or review artifact |

**Triage heuristic:** Does the change touch executable behavior? No → Lane 1. A small fix to
drive interactively from the hub session right now? → `/quick` (express-interactive). One
subtask, obvious approach, small diff, worth its own spoke session? → Lane 2. Otherwise → Lane 3.

### Micro-spoke lifecycle (lane 1)

Lane 1 is restricted to **non-executable paths only**: docs, comments, and wording.
It must never touch `scripts/`, `shared/hooks/`, `tests/`, or any skill script.

1. Hub spawns a subagent with `isolation: worktree` and a tight prompt (files, exact
   change, commit as `docs:`/`chore:` with plain `git add <files>` + standalone
   `git commit -m`, return branch + diff summary).
2. Subagent edits and commits inside the temp worktree. The `docs:`/`chore:` commit
   passes the `commit-quality` no-issue-anchor exemption because the entire staged set is
   non-executable documentation outside the restricted directories.
3. Hub reviews the diff.
4. Hub lands it:

   ```bash
   scripts/worktree-land.sh <branch> --local
   ```

   `--local` skips upstream guards (micro-spokes never push to origin), accepts a bare
   local branch whose temp worktree may already be gone, refuses any branch that has an
   upstream ("not a micro-spoke"), and refuses the default branch itself. No issue to
   close. The branch is deleted after the merge.

5. Cleanup: nothing is left behind — no branch, no worktree, no tmux window. None were
   created beyond the temp worktree managed by `isolation: worktree`.

**Hub-guard note.** `shared/hooks/hub-guard.sh` is path-anchored: it denies only on the
main checkout on the default branch and is a no-op in any linked worktree. A
worktree-isolated subagent therefore passes it by construction — no carve-outs exist or
are needed.

**No-issue commit path.** `docs:`/`chore:` commits whose entire staged set is
non-executable documentation (`.md`/`.markdown`/`.txt`/`.rst`, regular file mode,
outside `scripts/`, `shared/hooks/`, `tests/`, and any `*/scripts/` dir) pass the
`commit-quality` gate without an issue anchor — see that hook for the exact file-set
rules. This is the sanctioned path for lanes 1 and 2.

### Express-interactive lifecycle (`/quick`)

The express-interactive lane builds a small fix *conversationally from the hub session*
on its own branch/worktree — keeping the push gates, dropping the spoke process ceremony
(no issue, no `source-task`, no tmux session, no PLAN gate, no RED-first, no review
artifact). `main` is never edited directly.

1. From the hub: `scripts/worktree-quick.sh <slug>` (or `-t chore`). It creates a worktree
   on `quick/<slug>` (or `chore/<slug>`), copies `.claude/`, mints the `spoke_run_id`, sets
   the `.ai-toolkit/` exclude, and drops a `hub-guard-allow` marker in the common git-dir —
   then prints the worktree path. No issue, prompt, tmux window, or separate agent.
2. The **current session** `cd`s into the printed path and iterates: edit, run
   lint/typecheck/tests, commit. The marker lets the hub session commit into the worktree
   (hub-guard otherwise denies a commit run with the hub's cwd on the default branch).
3. Land from the hub: `scripts/worktree-land.sh <slug> --local` (unpushed) or
   `scripts/worktree-land.sh <slug>` (if pushed). A non-numbered branch needs no
   `ready/<issue>` marker and no `--force-land`. Teardown **revokes** the `hub-guard-allow`
   marker.

**Hub-guard note.** The `hub-guard-allow` marker bypasses *every* hub-guard check while
present (incl. on `main`) — the conscious escape hatch. It is a single global toggle, so a
teardown of one `/quick` lane revokes the bypass for any other concurrent lane until
re-granted; quick lanes are short interactive one-offs, so this is rarely a problem.

## Related

- `shared/skills/quick/SKILL.md` — the `/quick` express-interactive lane (above)
- `shared/skills/source-task/SKILL.md` — anchors a task to an issue; warns when you run
  `/source` on the shared main checkout instead of a worktree
- `shared/skills/solo-cycle/SKILL.md` — the per-subtask RED / GREEN / REVIEW / PUSH cycle
- `docs/metadata-and-sync.md` — how `shared/` propagates into the live `.claude/` config

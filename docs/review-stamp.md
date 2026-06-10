# Review stamp

HMAC-signed review-approval authenticator. The `review-stamp` MCP server lets
the `code-review` agent record its verdict as a **signed** artifact bound to the
exact diff it reviewed, so the push gate can mechanically distinguish "a review
of this diff happened" from "someone wrote a JSON file". Goal: reviewer ≠
author, enforced mechanically rather than by convention.

## How it works

1. The `code-review` agent calls the `approve_review` tool
   (`verdict: APPROVE | REQUEST_CHANGES`, `summary: string`).
2. The server (`mcp/review-stamp/server.py`, stdlib Python, newline-delimited
   JSON-RPC over stdio) resolves the repo, runs `git add -A`, computes the
   staged diff hash with the same algorithm as `review_diff_hash` in
   `shared/hooks/lib/utils.sh`, and writes `.review/<hash>.json` containing an
   HMAC-SHA256 signature over `<hash>:<verdict>`.
3. The `reviewer-sep-warn` push gate recomputes the hash of the pushed range
   and verifies the signature before allowing `git push` / `gh pr`.

The launcher `mcp/review-stamp/run.sh` loads the signing key from the macOS
Keychain (`security find-generic-password -a "$USER" -s REVIEW_STAMP_KEY -w`)
and exec's the server.

## Installation into target repos

`scripts/sync-to-repo.sh` copies the server (`run.sh` + `server.py`) into every
sync target at `.ai-toolkit/mcp/review-stamp/`, preserving the executable bit
on `run.sh` and recording both files in `.ai-toolkit-manifest.json` so stale
copies are garbage-collected like any other synced file. The `code-review`
agent frontmatter references the server as
`./.ai-toolkit/mcp/review-stamp/run.sh` — a repo-relative path that resolves in
any synced target (including this toolkit repo itself, which self-syncs).
`.ai-toolkit/` is gitignored: it is regenerated state, not source.

## One-time setup

Generate and store the signing key in the macOS Keychain:

```bash
security add-generic-password -a "$USER" -s REVIEW_STAMP_KEY -w "$(openssl rand -hex 32)"
```

## Enforcement per platform

| Platform | Mechanism | Strength |
| -------- | --------- | -------- |
| Claude Code | `review-stamp` MCP server wired only into the `code-review` agent frontmatter (`mcp-servers`) | Positive control — only the reviewer agent has the tool |
| Copilot | Same per-agent `mcp-servers` wiring in `.agent.md` frontmatter | Positive control — only the reviewer agent has the tool |
| Cursor | `beforeMCPExecution` guard (`review-stamp-guard.sh`, fail-closed) requires an active code-review review-window; signature verified at push by `reviewer-sep-warn` | Time-window + signature — see limitation below |

### Cursor specifics

Cursor cannot scope MCP servers per agent, and `readonly` mode blocks MCP
access entirely (which would brick the stamp tool), so `code-review` is **not**
marked readonly on Cursor. Instead:

- A `subagentStart` hook (`review-window-open.sh`) writes a `.review/.window`
  state file when a `code-review` subagent starts; a `subagentStop` hook
  (`review-window-close.sh`) removes it. The window expires after 30 minutes.
- The `beforeMCPExecution` guard (`review-stamp-guard.sh`, fail-closed) denies
  `approve_review` calls unless that window is active.

**Honest limitation:** `beforeMCPExecution` carries no agent identity. The
guard enforces a *time window* during which a code-review subagent is running,
not the identity of the caller. Positive caller control exists only on
Claude/Copilot via per-agent MCP wiring.

## Artifact schema

`.review/<hash>.json`:

```json
{
  "verdict": "APPROVE",
  "summary": "0 blockers, 1 warning, 2 nits — none",
  "reviewer": "code-review",
  "timestamp": "2026-06-10T10:00:00Z",
  "diff_hash": "<sha256 of the staged diff vs merge-base>",
  "signature": "<hex HMAC-SHA256 over '<hash>:<verdict>' keyed by REVIEW_STAMP_KEY>",
  "sig_alg": "HMAC-SHA256"
}
```

The push gate keys verification on three fields: `diff_hash` (must match the
pushed range), `verdict` (must be `APPROVE`), and `signature` (must verify
under `REVIEW_STAMP_KEY`). Other fields are informational; see
`mcp/review-stamp/server.py` for the authoritative shape.

## Threat model

What the signature buys: forging an approval now requires deliberately
extracting the signing key (from the Keychain or the environment) and
hand-rolling an HMAC — no longer a single innocent file write. An agent that
"accidentally" writes a plausible-looking artifact no longer passes the gate.

**Honest ceiling:** the same-user ceiling still applies. The author agent runs
as the same OS user, so it *could* read the key from the Keychain and sign its
own artifact. A local check cannot fully prove reviewer independence — only a
server-side check (key held off-machine) escapes that ceiling. Do not overclaim
the stamp as proof of separation; it raises the bar from "trivial" to
"deliberate forgery".

## Troubleshooting

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| Push gate warns it cannot verify the signature and names `REVIEW_STAMP_KEY` | No key resolvable (env or Keychain) — the gate falls back to an existence-only check | Run the one-time setup command above |
| Push blocked: invalid or missing signature | Artifact was hand-written, or the key changed since signing | Re-run the review so `approve_review` writes a freshly signed artifact |
| `approve_review` denied on Cursor | No active review window (`.review/.window` missing or older than 30 min) | Re-spawn the `code-review` subagent — the window opens at subagent start |
| Hash mismatch at push | Diff changed after review, or review-time and push-time merge-bases differ | Re-request review of the current diff; ensure the branch tracks an upstream |

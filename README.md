# ai-toolkit

Policy (rules, skills, agents, prompts) and a thin scripted lifecycle for AI coding agents on [Orca](https://github.com/stablyai/orca): Orca owns
execution, GitHub owns the backlog and the CI gate, ai-toolkit owns the policy and about two thousand lines of glue. Claude Code only.

- **Start here:** [`docs/architecture.md`](docs/architecture.md): lifecycle, coordinator runbook (`--reply`), setup on a new machine, known limits.
- **Into a project:** `scripts/sync.sh <repo> [--local-only]`, then `scripts/install.sh <repo>`, then `orca repo add --path <repo>`.
- **Run it:** file issues with a `Scope:` / `Gate:` footer (full cycle by default; `Gate: none` is the light lane, only when you ask for it), then `scripts/coordinator.sh --answer auto --drain` in an Orca terminal on the main checkout.
- **Test it:** `pip install -r requirements-dev.txt && pytest -n auto tests`; the real end-to-end is `e2e/github-scenario.sh`.

Layout: `shared/` (policy) · `scripts/` (dispatch, coordinator, review, answer, land, sync) · `hooks/` (Claude + git guards) · `bin/claude-spoke` (OTel launch shim) ·
`langfuse/` (collector config) · `settings/` (defaults) · `e2e/` · `tests/`.

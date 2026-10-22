---
name: jira
description: "Opt-in Jira Cloud adapter for POST_LAND_CMD: after a land, comment on the issue's Tracker: ticket and optionally move it to a named status. Use when a project tracks its tickets in Jira and asks to set up, configure or debug that update."
---
# Jira

`scripts/post-land.sh <issue> <tracker-key> <landed-sha>` is the project-side half of the `POST_LAND_CMD` contract: `land.sh` runs it after the base push for an issue that carries `Tracker: <KEY>`, with `BASE_BRANCH` in the environment. It calls the Jira Cloud REST API v3 with `curl` and `jq`, so it costs no tokens and needs no MCP session.

Nothing runs until a project sets `POST_LAND_CMD`, so the skill is inert in every other host.

## Opt in

1. Create an Atlassian API token (id.atlassian.com, Security, API tokens) and store it in the Keychain, never in a file:

   ```bash
   security add-generic-password -a "$USER" -s JIRA_API_TOKEN -w
   ```

2. In the project's `.ai-toolkit/ai-toolkit.local.env`:

   ```bash
   JIRA_SITE=rmcbfm.atlassian.net
   JIRA_EMAIL=you@example.com
   JIRA_LAND_TRANSITION=
   POST_LAND_CMD="bash .claude/skills/jira/scripts/post-land.sh"
   ```

   `JIRA_LAND_TRANSITION` is optional: a transition name (e.g. `Done`, matched case-insensitively) moves the ticket after the comment. Empty, the default, comments only.

## Behavior

- Validates the key (`^[A-Z][A-Z0-9_]+-[0-9]+$`) before any call.
- Posts "Landed on `<BASE_BRANCH>` in `<sha>` (GitHub `<owner/repo>#<issue>`); it reaches the next branch with its PR." on the ticket.
- With a transition name, reads `/transitions`, posts the matching id; an unknown name exits non-zero and lists the available names.
- Sends the credentials to `curl` on stdin (`--config -`), never in argv, with `--fail --max-time 20`.
- Exits non-zero with a one-line reason on missing config, a missing token or an HTTP error. `land.sh` turns that into a warning and an issue comment, never a failed land.

The Keychain must be readable by the process that runs the land (the coordinator loop or an attended `/land`).

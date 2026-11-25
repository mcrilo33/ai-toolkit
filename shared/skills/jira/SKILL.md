---
name: jira
description: "Opt-in Jira Cloud adapter for a Tracker: ticket: read it at intake, optionally move it when its issue is dispatched (POST_DISPATCH_CMD), comment on it and optionally move it after a land (POST_LAND_CMD). Use when a project tracks its tickets in Jira and asks to set up, configure or debug that."
---
# Jira

`scripts/post-land.sh <issue> <tracker-key> <landed-sha>` is the project-side half of the `POST_LAND_CMD` contract: `land.sh` runs it after the base push for an issue that carries `Tracker: <KEY>`, with `BASE_BRANCH` in the environment. It calls the Jira Cloud REST API v3 with `curl` and `jq`, so it costs no tokens and needs no MCP session.

`scripts/post-dispatch.sh <issue> <tracker-key>` is the start half (`POST_DISPATCH_CMD`, run by `dispatch.sh` after a first dispatch, never for a retry or an address round): it moves the ticket when `JIRA_START_TRANSITION` names a transition, else does nothing. `scripts/read-ticket.sh <tracker-key>` is the intake: `start-task` runs it to draft the issue from the ticket without the Atlassian connector, which is unreliable headless and sometimes down interactively.

Nothing runs until a project sets `POST_LAND_CMD` or `POST_DISPATCH_CMD` (`read-ticket.sh` only when called), so the skill is inert in every other host. The scripts share `scripts/jira-lib.sh` (config check, Keychain token, `api`, `transition_to`).

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
   JIRA_START_TRANSITION=
   POST_LAND_CMD="bash .claude/skills/jira/scripts/post-land.sh"
   POST_DISPATCH_CMD="bash .claude/skills/jira/scripts/post-dispatch.sh"
   ```

   `JIRA_LAND_TRANSITION` is optional: a transition name (e.g. `Done`, matched case-insensitively) moves the ticket after the comment. Empty, the default, comments only. `JIRA_START_TRANSITION` is the same for the start (e.g. `In Progress`): empty, the default, `post-dispatch.sh` makes no request.

## Behavior

- Validates the key (`^[A-Z][A-Z0-9_]+-[0-9]+$`) before any call.
- Posts "Landed on `<BASE_BRANCH>` in `<sha>` (GitHub `<owner/repo>#<issue>`); it reaches the next branch with its PR." on the ticket.
- With a transition name (`JIRA_LAND_TRANSITION`, or `JIRA_START_TRANSITION` in `post-dispatch.sh`), reads `/transitions`, posts the matching id; an unknown name exits non-zero and lists the available names.
- `read-ticket.sh <KEY>` reads `summary,description,issuetype,status` and prints them, the description flattened from ADF to plain text. Acceptance criteria are usually in the description; a site-specific custom field is for the PLAN to read, only if the site has one.
- Sends the credentials to `curl` on stdin (`--config -`), never in argv, with `--fail --max-time 20`.
- Exits non-zero with a one-line reason on missing config, a missing token or an HTTP error. `land.sh` turns that into a warning and an issue comment, never a failed land; `dispatch.sh` turns it into a warning, and the worker starts anyway.

The Keychain must be readable by the process that runs the land (the coordinator loop or an attended `/land`).

# Remote AFK on an always-on Mac

Run the unattended backlog drain (`/afk drain`, see the [`afk` skill](../shared/skills/afk/SKILL.md))
on a **second, always-on Mac** — triggered from anywhere, draining on its own using the
**same Claude Code subscription** (no API key, no LiteLLM proxy). Trigger it before you
leave; review the results on the [dashboard](../dashboard/README.md) when you get back.

This is a runbook: the one-time host setup, the cross-network trigger, and the fallback.
Monitoring while away is **not** a goal — the home Mac drains, and you read the outcome on
return.

> [!NOTE]
> The home Mac runs the same OS and toolkit as your local hub, so tooling and credentials
> behave identically. The only new concerns are **unattended survival** (stay awake, keep
> the Keychain unlocked) and a **cross-network trigger** (reach the Mac from a locked-down
> work network).

## How it works

`/afk --remote` (i.e. `hub-afk.sh --remote`) runs on your laptop/phone. It SSHes to the
configured host and starts a detached, `caffeinate`-wrapped drain in a named tmux session:

```text
ssh <host> "cd '<repo>' && tmux new -d -s '<session>' 'caffeinate -s bash shared/skills/hub/scripts/hub-afk.sh drain'"
```

It then confirms the tmux session came up and prints the reattach command. The drain runs
to backlog-empty on the home Mac, dispatching and landing spokes exactly as a local `/afk
drain` would — the spokes and the answerer authenticate with the subscription credentials
in the host's `~/.claude/`.

> [!NOTE]
> Verified (#85, smoke): a spoke parked at its PLAN gate is **auto-answered** by the afk reasoning answerer — not reaped — as of #84.

> [!IMPORTANT]
> The default launched command runs the supervisor **script** (`hub-afk.sh drain`), not
> `claude "/afk drain"`. A bare `claude` prompt opens an interactive session that would
> stall on a permission prompt before the drain is armed. Override `AFK_REMOTE_DRAIN_CMD`
> only if your host uses a different layout (e.g. a synced target's `.ai-toolkit/` path).

> [!NOTE]
> Two script paths appear below, by design. The **trigger machine** is a synced consumer
> repo, so it invokes `.ai-toolkit/scripts/hub/hub-afk.sh`. The **drain host** set up below
> is a raw `ai-toolkit` checkout, so its default drain command uses
> `shared/skills/hub/scripts/hub-afk.sh`. The launcher resolves both layouts.

## One-time host setup (the always-on Mac)

Do this once, logged in as the user that will run AFK.

### 1. Install the toolchain and the repo

```bash
# Claude Code, tmux, and gh (with a token that can read/write the repo)
brew install tmux gh
gh auth login                       # outbound HTTPS; grant repo scope
git clone <your-repo-url> ~/ai-toolkit
cd ~/ai-toolkit
./scripts/sync-to-repo.sh . claude  # generate the .claude/ config the spokes inherit
```

### 2. Log in with the subscription

```bash
claude            # then run /login → choose the subscription (device / OAuth flow)
```

Credentials persist in `~/.claude/` and auto-refresh, so subsequent headless runs reuse
them. While you are here, **allowlist the `Workflow` tool** so ultra spokes don't stall at
the dynamic-workflow gate (add `"Workflow"` to `permissions.allow` in the host's Claude
settings).

### 3. Auto-login so the login Keychain is unlocked on boot

Headless spokes can only read the subscription OAuth token when the **login Keychain is
unlocked**. The Keychain unlocks automatically only when the user logs in interactively, so
enable **automatic login** for the AFK user:

- **System Settings → Users & Groups → Automatically log in as → \<the AFK user\>**.

Run AFK as that same user. Without this, a spoke that starts after a reboot cannot
authenticate and the supervisor will block it (see [Auth failures](#auth-failures-during-a-run)).

### 4. Stay awake for the whole drain

A drain can run for hours; the Mac must not sleep.

- The launcher already wraps the drain in **`caffeinate -s`** (prevents idle sleep while
  the drain process lives).
- Keep the **lid open on AC power** (a closed lid sleeps a Mac laptop regardless of
  `caffeinate`, unless an external display is attached). A Mac mini / desktop is ideal.

## Trigger channel — reach the home Mac from another network

### Default: Tailscale mesh

Put the home Mac and your laptop/phone on a [Tailscale](https://tailscale.com/) network
(WireGuard, with NAT traversal and an HTTPS relay fallback that works through locked-down
corporate networks). No port forwarding, no public SSH exposure.

Configure the target once on the triggering machine — either as env vars or a sourced
`~/.afk-remote`:

```bash
# ~/.afk-remote  (sourced by the launcher; env vars override it)
AFK_REMOTE_HOST=mac-home          # the home Mac's Tailscale hostname
AFK_REMOTE_REPO=/Users/me/ai-toolkit
AFK_REMOTE_SESSION=afk            # optional (default: afk)
```

Then, from anywhere:

```bash
.ai-toolkit/scripts/hub/hub-afk.sh --remote
# → launching unattended drain on mac-home (tmux session 'afk')
# ✓ launched on mac-home — draining unattended until the backlog is empty
# Reattach with: ssh mac-home -t 'tmux attach -t afk'
```

To check on it (optional — monitoring is not the goal). `--status` is now a real liveness
check: it cross-checks the supervisor's heartbeat against pid liveness and reports `STALE`
when the window is armed but the process has died (issue #107), so you no longer need to
attach to the tmux session to tell a live drain from a crashed one. It must still run from
the repo dir, since it reads the checkout's state file:

```bash
ssh mac-home "cd '/Users/me/ai-toolkit' && bash shared/skills/hub/scripts/hub-afk.sh --status"
ssh mac-home -t 'tmux attach -t afk'   # or attach to watch it live; detach with Ctrl-b d
```

A crashed supervisor is also auto-restarted by the watchdog within one watchdog interval,
so a transient crash on the remote host recovers without intervention.

### Fallback: GitHub-poll (when the work network blocks Tailscale)

If the network blocks Tailscale outright, the home Mac can instead **poll outbound** for a
trigger signal and launch the drain itself — outbound HTTPS only, zero inbound exposure.

The design: a small launchd agent on the home Mac polls (via `gh`) for a trigger signal you
push from anywhere — for example an `afk-go` label on a sentinel issue, or a pushed `afk-go`
tag. On seeing it, the agent launches `hub-afk.sh drain` locally and clears the signal so it
fires once.

```text
you (any network) ──push `afk-go`──▶ GitHub ◀──poll (outbound HTTPS)── launchd agent on mac-home
                                                                            │
                                                                            └─▶ caffeinate -s hub-afk.sh drain
```

A sketch of the launchd agent (`~/Library/LaunchAgents/com.ai-toolkit.afk-poll.plist`) that
runs the poller every few minutes:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.ai-toolkit.afk-poll</string>
  <key>ProgramArguments</key> <array>
    <string>/bin/bash</string>
    <string>/Users/me/ai-toolkit/scripts/afk-poll.sh</string>
  </array>
  <key>StartInterval</key>    <integer>300</integer>
  <key>RunAtLoad</key>        <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.ai-toolkit.afk-poll.plist
```

> [!WARNING]
> The poller is a background daemon that can silently die (a crash, a `launchctl unload`, a
> revoked `gh` token). Treat Tailscale as the primary path and the poll as a documented
> backstop. `scripts/afk-poll.sh` itself is not shipped yet — this section specifies the
> contract so it can be added behind the fallback when needed.

## Auth failures during a run

The subscription token normally auto-refreshes. If it cannot (revoked login, a locked
Keychain after a reboot without auto-login), the supervisor detects the auth error from its
own `claude` call, emits `blocked/<issue>` for the affected spokes (so they surface on the
dashboard), and **stops** instead of spinning into dead credentials. To recover:

1. On the home Mac, run `claude` → `/login` and re-authenticate the subscription.
2. Confirm [auto-login](#3-auto-login-so-the-login-keychain-is-unlocked-on-boot) is on so a
   future reboot leaves the Keychain unlocked.
3. Re-trigger the drain (`/afk --remote`).

## Checklist

- [ ] Claude Code, `tmux`, and `gh` installed; repo cloned and synced on the host.
- [ ] `claude /login` done with the **subscription**; `Workflow` allowlisted.
- [ ] **Automatic login** enabled for the AFK user (Keychain unlocked on boot); AFK runs as
      that user.
- [ ] Lid open on AC (or a desktop Mac); the launcher's `caffeinate -s` keeps it awake.
- [ ] Tailscale up on both ends; `AFK_REMOTE_HOST` / `AFK_REMOTE_REPO` configured.
- [ ] One `/afk --remote` from another network launches a detached drain and prints the
      reattach command.

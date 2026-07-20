# AFK Travel

Toggle **travel mode** so an unattended `/afk` drain keeps running with the MacBook
**lid closed** and reachable over a **phone hotspot** — no second machine, no Tailscale.

Use it when the user says "travel mode", "I'm heading out", "going mobile", or "arrived
at my desk". It wraps one script — `scripts/afk-travel.sh` — and reports back.

## Why this exists

The local drain already inhibits *idle* and *system* sleep via `caffeinate -is`, but that
does **not** defeat **clamshell (lid-close) sleep** — close the lid and macOS sleeps
anyway. Travel mode flips the one extra lever, `pmset disablesleep`, and best-effort
switches Wi-Fi to a phone hotspot. Reaching *in* is not needed (you watch results on
GitHub from your phone), so there is no Tailscale/SSH channel to set up.

## Commands

```bash
scripts/afk-travel.sh on       # disable lid-close sleep + join the hotspot
scripts/afk-travel.sh off      # restore normal sleep
scripts/afk-travel.sh status   # SleepDisabled + power source + current SSID
```

- **on** — warns loudly if on battery (does not refuse — your call), runs
  `sudo pmset -a disablesleep 1`, and joins the Keychain-configured hotspot (or reminds
  you to switch by hand if none is set).
- **off** — `sudo pmset -a disablesleep 0`, restoring normal sleep.
- **status** — read-only; safe to run any time.

It toggles **sleep only** — it does **not** start the drain. Arm `/afk drain` first (or
confirm one is already running), then `on`, then close the lid.

## Workflow (what the agent does)

**"travel mode" / heading out:**

1. Confirm a drain is armed (`hub-afk.sh --status`); if not, arm `/afk drain` first.
2. Run `scripts/afk-travel.sh on`.
3. Tell the user: sleep is disabled, Wi-Fi is on the hotspot (or "switch to your phone
   Wi-Fi now"), the lid can close, and the drain keeps going. Remind them it's best on AC
   / a power bank — a closed lid running the suite on battery drains fast and heats up.

**"arrived at my desk" / back:**

1. Run `scripts/afk-travel.sh off` to restore normal sleep.
2. Ask whether to also stop the drain (`/afk off`) or leave it running now that you're back.

## One-time host setup

So the toggle runs hands-free (no per-invocation password prompt, no hardcoded secrets):

1. **Hotspot creds in the login Keychain** (never in a file):

   ```bash
   security add-generic-password -a "$USER" -s AFK_HOTSPOT_SSID     -w "<hotspot name>"
   security add-generic-password -a "$USER" -s AFK_HOTSPOT_PASSWORD -w "<hotspot password>"
   ```

   Absent creds are non-fatal — the script just asks you to switch Wi-Fi by hand.

2. **A narrow passwordless-sudo rule for `pmset` only** (`sudo visudo`):

   ```
   <you> ALL=(root) NOPASSWD: /usr/bin/pmset
   ```

   Without it, `sudo pmset` prompts for a password each time. Scoped to `pmset`, nothing
   else.

## Caveats

- **`pmset disablesleep` needs AC to be safe.** A closed MacBook running full pytest
  suites on battery drains in ~1-2h and can thermally throttle in a bag; dying mid-*land*
  can leave a half-merged state. Prefer AC / a power bank; best for short hops.
- **`disablesleep` stays set until `off`.** Always run `off` when back, or the machine
  won't sleep on its own.
- **SSID readout is best-effort** — newer macOS restricts it, so `status` may show an
  empty Wi-Fi line even though the join worked.
- **Verify once on your macOS.** `pmset disablesleep` behaviour has shifted across
  releases — close the lid on AC and confirm `hub-afk.sh --status` still ticks before
  relying on it for a trip.

## Related skills

- `afk` — the drain this keeps alive; travel mode does not start it, only keeps the lid
  from sleeping the machine.
- `hub` — orient and survey; check drain progress from GitHub while mobile.

# Travel-local: drain lid-closed on the iPhone hotspot

Keep an unattended `/afk` drain running on **this** Mac while you carry it lid-closed in a
bag on a short trip, tethered to your iPhone's Personal Hotspot — no second machine, no
migration. `travel-local on` gets the Mac travel-safe in one command; `travel-local off`
puts it back. This is the cheapest correct answer for **metro-scale trips** (under ~1 hour);
for flights or anything that powers the machine off, use the second-Mac
[remote-afk](./remote-afk.md) path instead.

> [!NOTE]
> The `/afk` machinery already tolerates connectivity gaps (a 5-minute supervisor tick, a
> 30-minute idle ceiling with revival, and a pre-reap auth probe), so the only missing
> pieces are OS-level: stay awake on a closed lid, and switch to the phone's Wi-Fi. That is
> all `travel-local` does.

## How it works

`scripts/travel-local.sh` (macOS only) is configured by a sourced `~/.afk-travel`, the same
pattern as `~/.afk-remote`:

```bash
# ~/.afk-travel
TRAVEL_HOTSPOT_SSID='Mathieu iPhone'   # required — your iPhone Personal Hotspot SSID
TRAVEL_HOME_SSID='HomeNet'             # optional — `off` rejoins it explicitly
```

Three verbs, both mutating verbs idempotent:

- **`on`** — join the hotspot (bounded retry), verify `https://api.anthropic.com` is
  reachable through the new interface, set `pmset -a disablesleep 1`, start a detached
  `caffeinate -s`, and confirm. If `caffeinate` cannot hold the machine, the disablesleep
  flip is rolled back rather than leaving the Mac half-configured.
- **`off`** — mirror the teardown: clear `disablesleep`, release `caffeinate`, restore
  Wi-Fi (rejoin `TRAVEL_HOME_SSID`, or cycle Wi-Fi so macOS auto-joins its preferred
  network), and **refresh the in-flight spokes' progress clocks** (see below).
- **`status`** — report disablesleep, caffeinate liveness, current SSID, connectivity, and
  the live `/afk` status in one screen.

### Why `off` refreshes the spoke clocks

A network blackout freezes each spoke's transcript — no writes reach it while the tether is
down. The `/afk` reaper reads that stale transcript mtime as an idle clock, so on the first
tick after you reconnect it could kill a spoke that was merely offline. `off` therefore
stamps **both** the progress epoch and the answer-attempt epoch for every in-flight spoke,
exactly as hub-afk's `resume_spoke` does — the answer-attempt epoch is the idle clock's
exclusion, so a just-reconnected spoke reads as busy until its next real write.

## One-time setup: passwordless `pmset`

`disablesleep` is the **only** switch that survives a closed lid on battery — `caffeinate`
alone does not. Setting it needs root, so for `travel-local on` to run non-interactively add
one line via `sudo visudo`, replacing `<user>` with your login name:

```text
<user> ALL=(root) NOPASSWD: /usr/bin/pmset
```

Without it, `on` and `off` prompt for a password (fine when attended; a blocker when you are
already walking out the door).

## Hotspot auto-join limits

- **The hotspot must be enabled on the phone.** Personal Hotspot only broadcasts its SSID
  while it is switched on (or, on newer iOS, while the Mac is a trusted device waking it).
  `on` retries the join for a bounded window and then fails with a clear "enable Personal
  Hotspot on the iPhone" message — enable it and re-run.
- **The network must already be known.** `networksetup -setairportnetwork` pulls the Wi-Fi
  password from the Keychain, so join the hotspot once by hand first. A never-before-seen
  SSID has no stored password and the join fails.
- **Connectivity is verified, not assumed.** Some macOS versions report a successful join
  even when association failed; `on` follows every join with a real reachability probe to
  `api.anthropic.com` and refuses to continue if it cannot reach it.

## Battery and thermal expectations

A Mac draining lid-closed in a bag is working hard with no airflow. Plan for it:

- **Battery drain is real.** A drain runs the CPU (spokes, the answerer, telemetry) with the
  display off but the SoC busy. Expect a meaningful battery hit over the trip; top up before
  you leave and keep the trip short.
- **Heat has nowhere to go.** A closed lid in a padded bag traps heat. macOS will thermally
  throttle before it damages anything, but a long, hot drain is slower and harder on the
  battery. This mode is sized for **sub-hour** trips, not an afternoon.
- **It is not silent-failure-proof.** If the phone battery dies, the hotspot drops, or the
  Mac thermally suspends, the drain pauses; the afk supervisor recovers what it can when
  connectivity returns, and `off`'s clock refresh keeps the reaper from over-reacting to the
  gap. Review the outcome on return — monitoring in transit is not a goal.

## When to prefer the second-Mac remote-afk path instead

Reach for [remote-afk](./remote-afk.md) (a second, always-on Mac) when travel-local's
trade-offs stop holding:

| Situation | Use |
|-----------|-----|
| Metro / errand, under ~1 hour, machine stays on | `travel-local` (this doc) |
| Flight, or any travel that powers the Mac off | [remote-afk](./remote-afk.md) |
| Multi-hour drain where battery/thermal is a risk | [remote-afk](./remote-afk.md) |
| You want to keep working on the laptop meanwhile | [remote-afk](./remote-afk.md) |

The second-Mac path costs minutes of handoff and a second machine, which is why it is the
wrong tool for a metro-scale trip — but it is the right one the moment the laptop must sleep,
power off, or stay free for other work.

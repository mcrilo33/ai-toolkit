# AFK arm-time self-check

What `/afk` verifies before it arms, and how to recover when a long unattended run loses
its credentials. Both apply to the local drain on this host (see the
[`afk` skill](../shared/skills/afk/SKILL.md)).

## Arm-time self-check

Before the first spoke dispatches, `/afk` runs a **liveness self-check**: real round trips
against the five dependencies the drain cannot work without. It exists because static checks
are not enough — on the 2026-07-13 drain the permission judge was structurally dead, every
static check passed, the drain armed clean, and every spoke's permissions ground to `DENY`
for about an hour before anyone noticed. That whole failure class is catchable in ~2 minutes
before a single spoke starts.

A healthy host logs one line and arms:

```text
/afk: arm self-check OK — judge alive (judge verdict: safe), claude alive, gh api reachable,
testmon present, telemetry wired (collector :4317, bridge :4319)
```

| Check | Policy | What a failure means |
|-------|--------|--------------------------------------------|
| Tier-3 permission judge | **Refuses to arm** | Every uncached tier-3 permission would fail closed to `DENY`. If the reason says *timed out*, the budget is too short for a `claude -p` cold start — raise `AFK_JUDGE_TIMEOUT`. If it says *unavailable*, the CLI or model is broken; the reason carries the judge's own error. |
| `claude` (answerer) | **Refuses to arm** | Reported as one of three states, because the remedies differ: `offline` (no network — nothing is wrong with your credentials), `auth-dead` (the token is dead — see [Auth failures](#auth-failures-during-a-run)), or unresponsive (no answer at all: `claude` absent from `PATH`, wedged, or slower than `AFK_ARM_AUTH_TIMEOUT`). |
| GitHub API | **Refuses to arm** | A bounded real `gh api` round trip failed. This is beyond `gh auth status`: the token can be valid while the API is unreachable (network, proxy, outage). Dispatch, land, and answer all need it. |
| `pytest-testmon` | **Warns, arms anyway** | Every first push per worktree runs the full multi-thousand-test suite instead of the affected set — slow, but still correct, so it never blocks. Fix with `pip install -r requirements-dev.txt`. |
| OTel collector + Langfuse bridge | **Refuses to arm** | The dashboard is the single source of truth for an unattended run. `AI_TOOLKIT_OTEL=0` drains without telemetry. |

On any refusal **no state is written** — the window is not half-armed, and nothing dispatches.
Fix the named dependency and re-arm `/afk`.

The check is **arm-only**. It deliberately does *not* run on `--reconcile` or a watchdog
respawn, because those are *recovery* paths: the watchdog recovers a crashed drain by running
`hub-afk.sh --reconcile` and discarding its output, so gating that on live probes would let a
transient outage — the very thing most likely to be happening around a crash — silently block
recovery and strand every in-flight spoke with no answerer or lander. Refusing to resume is
worse than resuming degraded, and a dependency that dies mid-window is already caught at
runtime by the judge halt and the auth halt.

Knobs: `AFK_ARM_SELFCHECK=0` skips every probe (independent of `AFK_ARM_PRECHECK`),
`AFK_ARM_AUTH_TIMEOUT` (default 120s) bounds the cold `claude` round trip, `AFK_JUDGE_TIMEOUT`
bounds the judge, and `AFK_GH_TIMEOUT` bounds the API call.

## Auth failures during a run

The subscription token normally auto-refreshes. If it cannot (revoked login, a locked
Keychain after a reboot without auto-login), the supervisor detects the auth error from its
own `claude` call, emits `blocked/<issue>` for the affected spokes (so they surface on the
dashboard), and **stops** instead of spinning into dead credentials. To recover:

1. On this host, run `claude` → `/login` and re-authenticate the subscription.
2. Enable automatic login (System Settings → Users & Groups) so a future reboot leaves
   the login Keychain unlocked.
3. Re-arm `/afk`.

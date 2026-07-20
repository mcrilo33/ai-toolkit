#!/usr/bin/env bash
#
# afk-travel.sh — toggle "travel mode" for an unattended /afk drain that must keep
# running with the MacBook lid CLOSED and reachable over a phone hotspot.
#
# The local drain already inhibits idle/system sleep via `caffeinate -is`, but that
# does NOT defeat CLAMSHELL (lid-close) sleep. This flips the one extra lever —
# `pmset disablesleep` — and, best-effort, switches Wi-Fi to a phone hotspot whose
# SSID/password live in the login Keychain (never hardcoded, per the secrets rule).
#
# Usage:
#   afk-travel.sh on       # disable lid-close sleep + join the hotspot
#   afk-travel.sh off      # restore normal sleep
#   afk-travel.sh status   # report SleepDisabled + power source + current SSID
#
# One-time host setup (so this runs hands-free):
#   security add-generic-password -a "$USER" -s AFK_HOTSPOT_SSID     -w "<ssid>"
#   security add-generic-password -a "$USER" -s AFK_HOTSPOT_PASSWORD -w "<password>"
#   sudo visudo   ->   <you> ALL=(root) NOPASSWD: /usr/bin/pmset
#
# It toggles SLEEP only; it does NOT start the drain. Arm `/afk drain` first, then
# `afk-travel.sh on`, then close the lid. Every external binary is overridable by
# env (AFK_PMSET_BIN / AFK_NETWORKSETUP_BIN / AFK_SECURITY_BIN / AFK_SUDO /
# AFK_WIFI_DEVICE) so the behaviour is testable without real hardware.
#
set -euo pipefail

PMSET="${AFK_PMSET_BIN:-/usr/bin/pmset}"
NETWORKSETUP="${AFK_NETWORKSETUP_BIN:-/usr/sbin/networksetup}"
SECURITY="${AFK_SECURITY_BIN:-/usr/bin/security}"
SUDO="${AFK_SUDO-sudo}"
WIFI_DEVICE="${AFK_WIFI_DEVICE:-en0}"

log() { printf '%s\n' "$*"; }
warn() { printf 'afk-travel: %s\n' "$*" >&2; }

# on_ac_power -> 0 when drawing from AC. `pmset -g ps` prints "... 'AC Power' ..."
# or "... 'Battery Power' ...". LC_ALL=C so the keyword match survives a non-C locale.
on_ac_power() {
  LC_ALL=C "$PMSET" -g ps 2>/dev/null | grep -q "AC Power"
}

# sleep_disabled -> the SleepDisabled flag (1 = clamshell sleep off), or empty.
sleep_disabled() {
  LC_ALL=C "$PMSET" -g 2>/dev/null | awk '/SleepDisabled/ {print $2}' || true
}

# current_ssid -> the joined Wi-Fi SSID, best-effort. Newer macOS restricts SSID
# readout from CLI tools, so this may be empty; it is cosmetic (the join still works).
current_ssid() {
  LC_ALL=C "$NETWORKSETUP" -getairportnetwork "$WIFI_DEVICE" 2>/dev/null \
    | sed -n 's/^Current Wi-Fi Network: //p' || true
}

keychain_secret() {
  "$SECURITY" find-generic-password -a "$USER" -s "$1" -w 2>/dev/null || true
}

# join_hotspot -> switch Wi-Fi to the Keychain-configured hotspot. No creds -> a
# loud reminder to switch by hand; never fails the caller.
join_hotspot() {
  local ssid pw
  ssid="$(keychain_secret AFK_HOTSPOT_SSID)"
  pw="$(keychain_secret AFK_HOTSPOT_PASSWORD)"
  if [ -z "$ssid" ]; then
    warn "no AFK_HOTSPOT_SSID in Keychain — switch Wi-Fi to your phone hotspot by hand"
    return 0
  fi
  if [ "$(current_ssid)" = "$ssid" ]; then
    log "  Wi-Fi already on '$ssid'"
    return 0
  fi
  if [ -n "$pw" ]; then
    "$NETWORKSETUP" -setairportnetwork "$WIFI_DEVICE" "$ssid" "$pw" >/dev/null 2>&1 \
      && log "  Wi-Fi -> '$ssid'" || warn "could not join '$ssid' — switch by hand"
  else
    "$NETWORKSETUP" -setairportnetwork "$WIFI_DEVICE" "$ssid" >/dev/null 2>&1 \
      && log "  Wi-Fi -> '$ssid'" || warn "could not join '$ssid' (no Keychain password) — switch by hand"
  fi
}

cmd_on() {
  if ! on_ac_power; then
    warn "ON BATTERY — a closed lid running the suite on battery drains fast and heats up."
    warn "Plug in (or a power bank) for a real window; continuing anyway — your call."
  fi
  $SUDO "$PMSET" -a disablesleep 1
  log "travel mode ON — lid-close sleep disabled (SleepDisabled=$(sleep_disabled))."
  join_hotspot
  log "You're good: close the lid, the drain keeps running. Run 'afk-travel.sh off' at your desk."
}

cmd_off() {
  $SUDO "$PMSET" -a disablesleep 0
  log "travel mode OFF — normal sleep restored (SleepDisabled=$(sleep_disabled))."
}

cmd_status() {
  local sd; sd="$(sleep_disabled)"; sd="${sd:-0}"
  if [ "$sd" = "1" ]; then
    log "SleepDisabled: 1 (lid-close sleep OFF — travel mode)"
  else
    log "SleepDisabled: ${sd} (normal sleep)"
  fi
  log "Power:         $(on_ac_power && echo AC || echo BATTERY)"
  log "Wi-Fi:         $(current_ssid || true)"
}

main() {
  case "${1:-}" in
    on)     cmd_on ;;
    off)    cmd_off ;;
    status) cmd_status ;;
    *) warn "usage: afk-travel.sh on|off|status"; exit 2 ;;
  esac
}

main "$@"

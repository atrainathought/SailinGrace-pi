#!/usr/bin/env bash
#
# Join a (password-protected) boat WiFi so the Pi can receive instrument
# data over UDP / TCP / SignalK — e.g. an MFD's WiFi network or the boat
# WiFi that Expedition's NMEA output rides on.
#
# Wraps `nmcli`. Credentials are stored by NetworkManager in
# /etc/NetworkManager/system-connections/ (root-only, 0600) — they are
# NEVER written into this repo or the deploy bundle. This file ships in a
# PUBLIC bundle, so do not hard-code any password here. Pass it at runtime
# or via a gitignored candidates file.
#
# Usage:
#   join_wifi.sh --scan                         # list visible networks
#   join_wifi.sh "Lynx MFD" "the-password"      # join one network
#   join_wifi.sh "Lynx MFD" --try creds.txt     # try each password in creds.txt until one works
#   join_wifi.sh --status                       # show the active WiFi connection
#   join_wifi.sh "Lynx MFD" "pw" --ifname wlan1 # use a specific radio (e.g. a USB dongle,
#                                               #   leaving wlan0 free to be your laptop's AP)
#
# creds.txt: one candidate password per line. Keep it OUT of git
#   (e.g. /home/pi/wifi_candidates.txt, chmod 600). Useful when you're
#   unsure of the exact variant (case / spacing / digits-vs-words).
#
# Single-radio note: the built-in WiFi can be a client of the boat network
# OR your laptop's access point, not both. To do both, join the boat WiFi
# on a second USB adapter (--ifname wlan1) and run the AP on wlan0; or give
# the laptop its link over ethernet / USB instead.

set -euo pipefail

IFACE="wlan0"
SSID=""
PASS=""
TRY_FILE=""
MODE="join"

while [ $# -gt 0 ]; do
  case "$1" in
    --scan)   MODE="scan" ;;
    --status) MODE="status" ;;
    --ifname) IFACE="${2:?--ifname needs a value}"; shift ;;
    --try)    TRY_FILE="${2:?--try needs a file}"; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  if [ -z "$SSID" ]; then SSID="$1"; elif [ -z "$PASS" ]; then PASS="$1"; fi ;;
  esac
  shift
done

command -v nmcli >/dev/null 2>&1 || {
  echo "ERROR: nmcli not found — this expects NetworkManager (Pi OS Bookworm/Trixie default)." >&2
  exit 3
}

case "$MODE" in
  scan)
    echo "Visible WiFi networks on $IFACE:"
    nmcli --fields SSID,SIGNAL,SECURITY device wifi list ifname "$IFACE" 2>/dev/null \
      || nmcli --fields SSID,SIGNAL,SECURITY device wifi list
    exit 0
    ;;
  status)
    echo "Active connections:"
    nmcli -t -f NAME,DEVICE,TYPE,STATE connection show --active | grep -i wifi || echo "  (no active WiFi)"
    echo "Current SSID on $IFACE: $(iwgetid -r "$IFACE" 2>/dev/null || echo '—')"
    exit 0
    ;;
esac

[ -n "$SSID" ] || { echo "ERROR: SSID required (or use --scan / --status)." >&2; exit 2; }

# Try to associate; returns 0 on success. Deletes a failed profile so the
# next attempt starts clean (a half-created connection blocks retries).
attempt() {
  local ssid="$1" pw="$2"
  if nmcli device wifi connect "$ssid" password "$pw" ifname "$IFACE" >/dev/null 2>&1; then
    return 0
  fi
  nmcli connection delete "$ssid" >/dev/null 2>&1 || true
  return 1
}

if [ -n "$TRY_FILE" ]; then
  [ -f "$TRY_FILE" ] || { echo "ERROR: candidates file not found: $TRY_FILE" >&2; exit 2; }
  echo "Trying candidate passwords for \"$SSID\" on $IFACE (from $TRY_FILE)…"
  n=0
  while IFS= read -r cand || [ -n "$cand" ]; do
    [ -z "$cand" ] && continue
    n=$((n + 1))
    printf "  [%d] trying… " "$n"          # never print the password itself
    if attempt "$SSID" "$cand"; then
      echo "connected ✓"
      echo "Joined \"$SSID\" on $IFACE (credentials stored by NetworkManager, not in the repo)."
      exit 0
    fi
    echo "no"
  done < "$TRY_FILE"
  echo "None of the $n candidates worked for \"$SSID\"." >&2
  exit 1
fi

[ -n "$PASS" ] || { echo "ERROR: password required (positional, or use --try <file>)." >&2; exit 2; }
if attempt "$SSID" "$PASS"; then
  echo "Joined \"$SSID\" on $IFACE (credentials stored by NetworkManager, not in the repo)."
  echo "Now run:  discover_signals.py scan   # the udp/tcp/signalk channels can see the network"
  exit 0
fi
echo "Failed to join \"$SSID\" — wrong password, out of range, or wrong --ifname." >&2
exit 1

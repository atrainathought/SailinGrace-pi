#!/usr/bin/env bash
#
# Package THIS repo's boat-side files into a tarball for no-git installs.
#
# This is the SailinGrace-pi repo — the source of truth for the boat-side
# relay appliance. The relay Pi never runs the SailinGrace routing app, so
# none of the routing / weather / RLSO code lives here. The bundle is just
# the signalk-server logger, the UPS monitor, signal discovery, the wifi
# join helper, the config UI, and the setup/uninstall scripts (~60 KB).
# That is the whole point: nothing to protect, nothing to leak.
#
# Two output modes:
#
#   # 1. Tarball (no git on the Pi) — default:
#   scripts/build_pi_bundle.sh
#   scp dist/sailingrace-pi.tar.gz pi@<pi-host>:~
#   ssh pi@<pi-host> 'tar xzf sailingrace-pi.tar.gz && bash SailinGrace/scripts/setup_pi.sh'
#
#   # 2. Write the unpacked tree into a directory:
#   scripts/build_pi_bundle.sh --dir /path/to/out
#
# Flags:
#   --dir DIR   write the unpacked tree into DIR instead of a tarball
#   --tar PATH  tarball output path (default: dist/sailingrace-pi.tar.gz)
#   --sim       also include the fake-boat simulators (for a test Pi)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

INCLUDE_SIM=0
OUT_DIR=""
OUT_TAR="$REPO_ROOT/dist/sailingrace-pi.tar.gz"
while [ $# -gt 0 ]; do
  case "$1" in
    --sim) INCLUDE_SIM=1 ;;
    --dir) OUT_DIR="${2:?--dir needs a path}"; shift ;;
    --tar) OUT_TAR="${2:?--tar needs a path}"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# Complete relay-Pi file set, repo-relative. The scripts/ + scripts/pi/ +
# scripts/systemd/ layout is preserved so setup_pi.sh finds everything
# where it expects (it derives REPO_ROOT from its own location).
FILES=(
  scripts/setup_pi.sh
  scripts/uninstall.sh
  scripts/capture_signalk.py
  scripts/capture_nmea.py
  scripts/discover_signals.py
  scripts/ups_monitor.py
  scripts/pi/log_signalk.sh
  scripts/pi/sailingrace-logger.service
  scripts/pi/join_wifi.sh
  scripts/pi/config_server.py
  scripts/systemd/ups-monitor.service
)
if [ "$INCLUDE_SIM" = 1 ]; then
  FILES+=( scripts/nmea0183_simulator.py scripts/signalk_simulator.py )
fi

for f in "${FILES[@]}"; do
  [ -f "$REPO_ROOT/$f" ] || { echo "missing file: $f" >&2; exit 1; }
done

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
pkg="$stage/SailinGrace"        # top-level dir name the units expect
for f in "${FILES[@]}"; do
  mkdir -p "$pkg/$(dirname "$f")"
  cp "$REPO_ROOT/$f" "$pkg/$f"
done

cat > "$pkg/README.md" <<'MD'
# SailinGrace — Pi relay bundle

Boat-side **relay** files only: the signalk-server delta logger, the UPS
monitor, and setup/uninstall. This does **not** contain the SailinGrace
routing app — that runs on a laptop and connects to this Pi over SignalK.

Deploy:
    bash scripts/setup_pi.sh        # installs venv + deps + units, enables logger
                                    # then follow the printed signalk-server steps
Remove:
    sudo bash scripts/uninstall.sh --confirm
MD

if [ -n "$OUT_DIR" ]; then
  mkdir -p "$OUT_DIR"
  cp -a "$pkg/." "$OUT_DIR/"
  echo "Pi bundle synced to: $OUT_DIR"
  echo "  files: ${#FILES[@]} + README.md"
else
  mkdir -p "$(dirname "$OUT_TAR")"
  tar -C "$stage" -czf "$OUT_TAR" SailinGrace
  echo "Pi bundle: $OUT_TAR  ($(du -h "$OUT_TAR" | cut -f1))"
  echo
  echo "Copy to the Pi (no git needed):"
  echo "  scp \"$OUT_TAR\" pi@<pi-host>:~"
  echo "  ssh pi@<pi-host> 'tar xzf $(basename "$OUT_TAR") && bash SailinGrace/scripts/setup_pi.sh'"
fi

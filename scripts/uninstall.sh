#!/usr/bin/env bash
#
# Remove SailinGrace from a boat-side install (Pi or laptop).
#
# What it does (each step idempotent, prints what it touches):
#   0. With --archive-logs / --archive-dir: archives first (step 3 below)
#   1. Stops + disables sailingrace + sailingrace-logger + ups-monitor
#      systemd units; removes the unit files
#   2. Removes the data/license.json + data/settings.json overrides
#      (operator-editable config — anything actually generated stays)
#   3. Optionally archives ~/sailingrace-logs/ + data/ to
#      <dir>/sailingrace-uninstall-<date>/ for the operator to copy off
#      before final delete. <dir> is your home by default, or a USB
#      stick with --archive-dir (the logs can be GBs, and a copy on the
#      same SD card often won't fit). Space is checked before anything is
#      removed, the copy runs before any removal, and a failed copy
#      stops the script with nothing removed.
#      Never /tmp: it is a RAM-backed tmpfs on Trixie.
#   4. Optionally removes the repo clone (asked, not assumed)
#
# What it does NOT do:
#   - Touch signalk-server, can-utils, Node.js, Python, system packages
#     (those were here before SailinGrace and may be used by other tools)
#   - Remove pyenv / venv / pip caches in $HOME (operator's user-level
#     tooling, not SailinGrace's to delete)
#   - Modify Windows-side anything (netsh portproxy / firewall rules);
#     run those from PowerShell separately if installed via wsl2.md
#   - Revert setup_pi.sh's system changes (you may still want them):
#     config.txt PiCAN-M + USB-C gadget blocks, cmdline.txt
#     modules-load=dwc2,g_ether, /etc/systemd/network/80-can.network,
#     the "sg-usb" NetworkManager connection, signalk-server "sg-*"
#     connections (remove the last two via nmcli / the admin UI)
#
# Usage:
#   sudo bash scripts/uninstall.sh                    # dry-run preview
#   sudo bash scripts/uninstall.sh --confirm          # actually remove
#   sudo bash scripts/uninstall.sh --confirm --archive-logs
#   sudo bash scripts/uninstall.sh --confirm --archive-dir /media/$USER/STICK
#   sudo bash scripts/uninstall.sh --confirm --remove-repo

set -uo pipefail

CONFIRM=0
ARCHIVE=0
REMOVE_REPO=0
ARCHIVE_TO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)       CONFIRM=1 ;;
    --archive-logs)  ARCHIVE=1 ;;
    --archive-dir)   ARCHIVE=1; ARCHIVE_TO="${2:?--archive-dir needs a directory}"; shift ;;
    --remove-repo)   REMOVE_REPO=1 ;;
    -h|--help)
      head -39 "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "unknown flag: $1 (use --help)" >&2
      exit 2
      ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Run via sudo, so use the invoking user's home (not /root, and not an
# assumed /home/pi — Pi OS no longer ships a default `pi` user).
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUN_HOME="${RUN_HOME:-$HOME}"

# Archive pre-flight: check there's room BEFORE removing anything.
ARCHIVE_SRCS=()
if [[ "$ARCHIVE" -eq 1 ]]; then
  ARCHIVE_TO="${ARCHIVE_TO:-$RUN_HOME}"
  if [[ ! -d "$ARCHIVE_TO" ]]; then
    echo "archive directory not found: $ARCHIVE_TO" >&2
    exit 1
  fi
  for src in "$RUN_HOME/sailingrace-logs" "$REPO_ROOT/data"; do
    [[ -d "$src" ]] && ARCHIVE_SRCS+=("$src")
  done
  need_mb=0
  if [[ ${#ARCHIVE_SRCS[@]} -gt 0 ]]; then
    need_mb=$(du -smc "${ARCHIVE_SRCS[@]}" 2>/dev/null | tail -1 | cut -f1)
  fi
  free_mb=$(df -Pm "$ARCHIVE_TO" | tail -1 | awk '{print $4}')
  echo "archive: ${need_mb} MB to copy, ${free_mb} MB free on $ARCHIVE_TO"
  if (( need_mb + 200 > free_mb )); then
    echo "not enough space for the archive on $ARCHIVE_TO (need ${need_mb} MB + 200 MB margin)." >&2
    echo "→ plug in a USB stick and use --archive-dir /media/$RUN_USER/<stick>, or copy the logs off first." >&2
    echo "Nothing was removed." >&2
    exit 1
  fi
fi

if [[ "$CONFIRM" -eq 0 ]]; then
  echo "── DRY RUN ── pass --confirm to actually do this ─────────────"
fi
do_or_say() {
  local label=$1; shift
  echo "  $label"
  if [[ "$CONFIRM" -eq 1 ]]; then
    "$@"
  fi
}

# ── 1. archive logs + data (opt-in) — FIRST, before anything is removed ──

if [[ "$ARCHIVE" -eq 1 ]]; then
  ARCHIVE_DIR="$ARCHIVE_TO/sailingrace-uninstall-$(date -u +%Y%m%dT%H%M%SZ)"
  echo
  echo "── archive logs + DB to $ARCHIVE_DIR ─────────────────────────"
  do_or_say "mkdir -p $ARCHIVE_DIR" mkdir -p "$ARCHIVE_DIR"
  for src in "${ARCHIVE_SRCS[@]}"; do
    if ! do_or_say "cp -r $src $ARCHIVE_DIR/" cp -r "$src" "$ARCHIVE_DIR/"; then
      echo "  !! copying $src failed — the archive in $ARCHIVE_DIR is INCOMPLETE." >&2
      echo "  !! stopping here; nothing has been removed yet. Free space or use --archive-dir." >&2
      exit 1
    fi
  done
  if [[ "$CONFIRM" -eq 1 ]]; then
    chown -R "$RUN_USER": "$ARCHIVE_DIR" 2>/dev/null || true
    echo "  → archived to $ARCHIVE_DIR — copy this off the boat"
  fi
fi

# ── 2. systemd units ────────────────────────────────────────────────

echo
echo "── systemd units ─────────────────────────────────────────────"
# sailingrace = the retired all-in-one backend unit, removed if an old install left it.
for unit in sailingrace sailingrace-logger sailingrace-config ups-monitor; do
  unit_path="/etc/systemd/system/${unit}.service"
  if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}.service"; then
    do_or_say "stop ${unit}.service"     sudo systemctl stop "${unit}.service" 2>/dev/null
    do_or_say "disable ${unit}.service"  sudo systemctl disable "${unit}.service" 2>/dev/null
  fi
  if [[ -f "$unit_path" ]]; then
    do_or_say "remove $unit_path"        sudo rm -f "$unit_path"
  fi
done
do_or_say "systemctl daemon-reload"      sudo systemctl daemon-reload

# ── 3. operator-editable config overrides ───────────────────────────

echo
echo "── runtime config overrides (UI-edited; not generated data) ──"
for f in "$REPO_ROOT/data/license.json" "$REPO_ROOT/data/settings.json"; do
  if [[ -f "$f" ]]; then
    do_or_say "remove $f" rm -f "$f"
  fi
done
if [[ -d /etc/sailingrace ]]; then
  do_or_say "remove /etc/sailingrace (config UI setup token)" sudo rm -rf /etc/sailingrace
fi
if [[ -f /etc/udev/rules.d/86-sailingrace-usb0.rules ]]; then
  do_or_say "remove usb0 NetworkManager udev override" sudo rm -f /etc/udev/rules.d/86-sailingrace-usb0.rules
fi

# ── 4. repo removal (opt-in, explicit) ──────────────────────────────

if [[ "$REMOVE_REPO" -eq 1 ]]; then
  echo
  echo "── repo clone ────────────────────────────────────────────────"
  do_or_say "rm -rf $REPO_ROOT" rm -rf "$REPO_ROOT"
fi

echo
if [[ "$CONFIRM" -eq 0 ]]; then
  echo "── DRY RUN done. Re-run with --confirm to actually remove. ──"
else
  echo "── Uninstall complete. ──────────────────────────────────────"
  echo "Not touched (intentional): signalk-server, Node.js, system Python,"
  echo "user venvs, OS packages, and setup_pi.sh's boot/network changes"
  echo "(listed in this script's header). Remove those separately if unused."
fi

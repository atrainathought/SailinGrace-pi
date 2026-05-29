#!/usr/bin/env bash
#
# Remove SailinGrace from a boat-side install (Pi or laptop).
#
# What it does (each step idempotent, prints what it touches):
#   1. Stops + disables sailingrace + sailingrace-logger + ups-monitor
#      systemd units; removes the unit files
#   2. Removes the data/license.json + data/settings.json overrides
#      (operator-editable config — anything actually generated stays)
#   3. Optionally archives data/sailingrace-logs/ + data/*.db to
#      /tmp/sailingrace-uninstall-<date>/ for the operator to copy off
#      before final delete
#   4. Optionally removes the repo clone (asked, not assumed)
#
# What it does NOT do:
#   - Touch signalk-server, can-utils, Node.js, Python, system packages
#     (those were here before SailinGrace and may be used by other tools)
#   - Remove pyenv / venv / pip caches in $HOME (operator's user-level
#     tooling, not SailinGrace's to delete)
#   - Modify Windows-side anything (netsh portproxy / firewall rules);
#     run those from PowerShell separately if installed via wsl2.md
#
# Usage:
#   sudo bash scripts/uninstall.sh                    # dry-run preview
#   sudo bash scripts/uninstall.sh --confirm          # actually remove
#   sudo bash scripts/uninstall.sh --confirm --archive-logs
#   sudo bash scripts/uninstall.sh --confirm --remove-repo

set -uo pipefail

CONFIRM=0
ARCHIVE=0
REMOVE_REPO=0
for arg in "$@"; do
  case "$arg" in
    --confirm)       CONFIRM=1 ;;
    --archive-logs)  ARCHIVE=1 ;;
    --remove-repo)   REMOVE_REPO=1 ;;
    -h|--help)
      head -30 "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg (use --help)" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

# ── 1. systemd units ────────────────────────────────────────────────

echo
echo "── systemd units ─────────────────────────────────────────────"
for unit in sailingrace sailingrace-logger ups-monitor; do
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

# ── 2. operator-editable config overrides ───────────────────────────

echo
echo "── runtime config overrides (UI-edited; not generated data) ──"
for f in "$REPO_ROOT/data/license.json" "$REPO_ROOT/data/settings.json"; do
  if [[ -f "$f" ]]; then
    do_or_say "remove $f" rm -f "$f"
  fi
done

# ── 3. archive logs + db (opt-in) ───────────────────────────────────

if [[ "$ARCHIVE" -eq 1 ]]; then
  ARCHIVE_DIR="/tmp/sailingrace-uninstall-$(date -u +%Y%m%dT%H%M%SZ)"
  echo
  echo "── archive logs + DB to $ARCHIVE_DIR ─────────────────────────"
  do_or_say "mkdir -p $ARCHIVE_DIR" mkdir -p "$ARCHIVE_DIR"
  for src in /home/pi/sailingrace-logs "$REPO_ROOT/data"; do
    if [[ -d "$src" ]]; then
      do_or_say "cp -r $src $ARCHIVE_DIR/" cp -r "$src" "$ARCHIVE_DIR/" || true
    fi
  done
  if [[ "$CONFIRM" -eq 1 ]]; then
    echo "  → archived to $ARCHIVE_DIR — copy this off the boat before continuing"
  fi
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
  echo "user venvs, OS packages. Remove those separately if no longer used."
fi

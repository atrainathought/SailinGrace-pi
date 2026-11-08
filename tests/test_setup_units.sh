#!/usr/bin/env bash
# setup_pi.sh's install_unit rewrites the shipped /home/pi defaults to the
# real repo path, login user and home. Run the real function with sudo/tee
# stubbed out, for a non-pi user whose repo happens to live under /home/pi.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$(mktemp -d)"; trap 'rm -rf "$out"' EXIT

fn="$(sed -n '/^sed_repl()/p; /^install_unit() {/,/^}/p' "$here/scripts/setup_pi.sh")"
[ -n "$fn" ] || { echo "install_unit not found in setup_pi.sh"; exit 1; }
sudo() { if [ "$1" = tee ]; then cat > "$out/$(basename "$2")"; else "$@"; fi; }
eval "$fn"

# shellcheck disable=SC2034  # read by the eval'd install_unit
REPO_ROOT=/home/pi/projects/SailinGrace-pi RUN_USER=alice RUN_HOME=/home/alice
for u in pi/sailingrace-logger.service pi/sailingrace-config.service systemd/ups-monitor.service; do
  install_unit "$here/scripts/$u"
done

fail=0
check() { grep -qx -- "$2" "$out/$1" || { echo "FAIL $1: missing '$2'"; fail=1; }; }
check sailingrace-logger.service "User=alice"
check sailingrace-logger.service "Environment=LOG_DIR=/home/alice/sailingrace-logs"
check sailingrace-logger.service "Environment=SG_REPO=/home/pi/projects/SailinGrace-pi"
check sailingrace-logger.service "ExecStart=/home/pi/projects/SailinGrace-pi/scripts/pi/log_signalk.sh"
check sailingrace-config.service "User=root"
check sailingrace-config.service "Environment=SIGNALK_NODE_SETTINGS=/home/alice/.signalk/settings.json"
check sailingrace-config.service "Environment=LOG_DIR=/home/alice/sailingrace-logs"
check ups-monitor.service "ExecStart=/home/pi/projects/SailinGrace-pi/.venv/bin/python /home/pi/projects/SailinGrace-pi/scripts/ups_monitor.py"
if grep -n '^[^#].*/home/pi/SailinGrace\b' "$out"/*.service; then echo "FAIL: unrewritten /home/pi/SailinGrace path"; fail=1; fi

# Awkward paths: '&' and '#' in the repo path, and /home/pi-data must not
# be mistaken for /home/pi.
# shellcheck disable=SC2034  # read by the eval'd install_unit
REPO_ROOT='/home/bob/boat&sail #2/SailinGrace-pi' RUN_USER=bob RUN_HOME=/home/bob
cat > "$out/src-odd.service" <<'UNIT'
User=pi
ExecStart=/home/pi/SailinGrace/scripts/pi/log_signalk.sh
Environment=LOG_DIR=/home/pi/sailingrace-logs
Environment=SPARE=/home/pi-data/x
Environment=HOME_ONLY=/home/pi
UNIT
install_unit "$out/src-odd.service"
check src-odd.service "User=bob"
check src-odd.service "ExecStart=/home/bob/boat&sail #2/SailinGrace-pi/scripts/pi/log_signalk.sh"
check src-odd.service "Environment=LOG_DIR=/home/bob/sailingrace-logs"
check src-odd.service "Environment=SPARE=/home/pi-data/x"
check src-odd.service "Environment=HOME_ONLY=/home/bob"

[ "$fail" = 0 ] && echo "install_unit: all rewrites OK"
exit "$fail"
